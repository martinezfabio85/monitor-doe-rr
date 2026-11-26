"""
Monitor do Diário Oficial do Estado de Roraima (DOE-RR).

1. Abre o portal da Imprensa Oficial num navegador real (Playwright/Chromium).
2. Lê a tabela "ÚLTIMAS PUBLICAÇÕES" e identifica edições ainda não processadas.
3. Baixa cada PDF novo, extrai o texto e procura os termos de termos.txt.
4. Abre uma Issue no GitHub com o resultado (o GitHub avisa por e-mail e no app).
5. Guarda em estado.json o que já foi processado.

Variáveis de ambiente (preenchidas pelo GitHub Actions):
  GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_REPOSITORY_OWNER
  ALERTA_SEM_MENCAO = "1" para abrir Issue mesmo quando nenhum termo aparecer
  MAX_EDICOES       = quantas edições novas processar por execução (padrão 5)
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PORTAL = "https://www.imprensaoficial.rr.gov.br/_inicial/"
RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "estado.json"
TERMOS = RAIZ / "termos.txt"
PASTA_PDF = RAIZ / "pdfs"            # todos os PDFs baixados (vão como artefato)
PASTA_GUARDADOS = RAIZ / "doe"       # PDFs com menção a NOME (ficam no repositório)
PASTA_DEBUG = RAIZ / "debug"
MAX_EDICOES = int(os.getenv("MAX_EDICOES", "5"))
RE_DATA = re.compile(r"(\d{2})\s*/\s*(\d{2})\s*/\s*(\d{4})")


# ---------------------------------------------------------------- utilidades
def normalizar(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = re.sub(r"-\s*\n\s*", "", txt)          # palavra hifenizada na quebra de linha
    return re.sub(r"\s+", " ", txt).upper()


def carregar_termos():
    termos = []
    for linha in TERMOS.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "|" not in linha:
            continue
        cat, termo = (p.strip() for p in linha.split("|", 1))
        termos.append((cat.upper(), termo, normalizar(termo)))
    return termos


def carregar_estado():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"processadas": []}


def salvar_estado(estado):
    estado["processadas"] = estado["processadas"][-300:]
    estado["ultima_execucao"] = datetime.now().isoformat(timespec="seconds")
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------ leitura do site
PORTAIS = [
    PORTAL,
    "https://imprensaoficial.rr.gov.br/_inicial/",
    "https://www.imprensaoficial.rr.gov.br/",
]


def _linhas_com_data(frame):
    """Procura linhas de tabela (ou itens parecidos) com data dd / mm / aaaa num frame."""
    achadas = []
    for seletor in ("tr", "li", "div.row", "[class*=item]"):
        loc = frame.locator(seletor)
        try:
            n = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                texto = el.inner_text(timeout=3_000)
            except Exception:  # noqa: BLE001
                continue
            if RE_DATA.search(texto) and "MB" in texto.upper() and len(texto) < 400:
                achadas.append((el, texto))
        if achadas:
            break
    return achadas


def diagnostico(page):
    print("URL:", page.url, "| título:", page.title())
    print("Frames:", [f.url for f in page.frames])
    try:
        corpo = page.locator("body").inner_text(timeout=5_000)
        print("Texto da página (início):", re.sub(r"\s+", " ", corpo)[:1500])
    except Exception as e:  # noqa: BLE001
        print("Sem texto:", e)


def listar_edicoes(page):
    """Retorna [{'data': 'dd/mm/aaaa', 'chave': str, 'linha': Locator, 'href': str|None}]"""
    linhas = []
    for url in PORTAIS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_load_state("networkidle", timeout=60_000)
        except PWTimeout:
            pass
        except Exception as e:  # noqa: BLE001
            print("Falha ao abrir", url, e)
            continue
        for _ in range(6):  # até ~30 s esperando a tabela aparecer
            for frame in page.frames:
                linhas = _linhas_com_data(frame)
                if linhas:
                    break
            if linhas:
                break
            page.wait_for_timeout(5_000)
        if linhas:
            print("Tabela encontrada em", page.url)
            break
        diagnostico(page)

    edicoes = []
    for linha, texto in linhas:
        m = RE_DATA.search(texto)
        data = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
        href = None
        for a in linha.locator("a[href]").all():
            h = (a.get_attribute("href") or "").strip()
            if h and not h.startswith(("#", "javascript")):
                href = urljoin(page.url, h)
        tamanho = re.search(r"([\d.,]+\s*MB)", texto, re.I)
        chave = href or f"{data}|{tamanho.group(1) if tamanho else texto.strip()[:60]}"
        edicoes.append({"data": data, "chave": chave, "linha": linha, "href": href})

    edicoes.sort(key=lambda e: datetime.strptime(e["data"], "%d/%m/%Y"), reverse=True)
    return edicoes


def baixar_pdf(page, context, edicao, destino: Path) -> str:
    """Baixa o PDF da edição. Retorna a URL do arquivo (quando conhecida)."""
    url = edicao["href"]

    if not url:
        # Sem href: clicar no último elemento clicável da linha ("Visualizar")
        alvo = edicao["linha"].locator("a, button, i, img, [onclick]").last
        try:
            with page.expect_download(timeout=30_000) as dl:
                alvo.click()
            dl.value.save_as(destino)
            return dl.value.url
        except PWTimeout:
            pass
        try:
            with context.expect_page(timeout=30_000) as nova:
                alvo.click()
            pop = nova.value
            pop.wait_for_load_state("load")
            url = pop.url
            pop.close()
        except PWTimeout:
            raise RuntimeError(f"Não consegui abrir o arquivo da edição de {edicao['data']}")

    resp = context.request.get(url, timeout=180_000)
    corpo = resp.body()
    if not corpo.startswith(b"%PDF"):
        # Página intermediária (visualizador): procurar link .pdf dentro dela
        html = corpo.decode("utf-8", "ignore")
        m = re.search(r"""["']([^"']+\.pdf[^"']*)["']""", html, re.I)
        if not m:
            raise RuntimeError(f"O link da edição de {edicao['data']} não devolveu um PDF: {url}")
        url = urljoin(url, m.group(1))
        corpo = context.request.get(url, timeout=180_000).body()
    destino.write_bytes(corpo)
    return url


# ---------------------------------------------------------- análise do texto
def paginas_do_pdf(caminho: Path):
    saida = subprocess.run(
        ["pdftotext", "-layout", str(caminho), "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    paginas = saida.split("\f")
    return paginas[:-1] if paginas and not paginas[-1].strip() else paginas


def procurar(paginas, termos):
    achados = []  # (categoria, termo, pagina, trecho)
    for n, bruto in enumerate(paginas, start=1):
        texto = normalizar(bruto)
        for cat, termo, alvo in termos:
            for m in re.finditer(re.escape(alvo), texto):
                ini, fim = max(0, m.start() - 220), min(len(texto), m.end() + 220)
                achados.append((cat, termo, n, "…" + texto[ini:fim].strip() + "…"))
    return achados


def numero_edicao(paginas):
    m = re.search(r"EDI[CÇ][AÃ]O\s*N[°ºO]?\s*:?\s*(\d+)", normalizar(" ".join(paginas[:3])))
    return m.group(1) if m else None


# ------------------------------------------------------------- notificação
def abrir_issue(titulo, corpo, rotulos=()):
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        print("\n=== (sem GITHUB_TOKEN) ===\n", titulo, "\n", corpo)
        return
    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": titulo, "body": corpo, "labels": list(rotulos)},
        timeout=30,
    )
    if r.status_code >= 300:
        print("Falha ao abrir Issue:", r.status_code, r.text)


def montar_relatorio(edicao, num, url, total_pag, achados):
    dono = os.getenv("GITHUB_REPOSITORY_OWNER", "")
    nomes = [a for a in achados if a[0] == "NOME"]
    outros = [a for a in achados if a[0] != "NOME"]

    if nomes:
        pessoas = sorted({a[1] for a in nomes})
        titulo = f"🔴 DOE {edicao['data']}: {', '.join(pessoas)} citado(s)"
    elif outros:
        titulo = f"🟡 DOE {edicao['data']}: SEPLAN ({len(outros)} menções)"
    else:
        titulo = f"⚪ DOE {edicao['data']}: nenhum termo encontrado"

    linhas = [
        f"@{dono}" if dono else "",
        f"**Edição nº {num or '?'} — {edicao['data']}** · {total_pag} páginas",
        f"Arquivo: {url}" if url else "",
        "",
    ]
    if nomes:
        linhas.append("## Nomes")
        for cat, termo, pag, trecho in nomes:
            linhas += [f"**{termo}** — página {pag}", f"> {trecho}", ""]
    if outros:
        cats = sorted({a[0] for a in outros})
        for c in cats:
            itens = [a for a in outros if a[0] == c]
            paginas = sorted({a[2] for a in itens})
            linhas.append(f"## {c} — {len(itens)} menções nas páginas {', '.join(map(str, paginas))}")
            for cat, termo, pag, trecho in itens[:15]:
                linhas += [f"Página {pag}:", f"> {trecho}", ""]
            if len(itens) > 15:
                linhas.append(f"_…e mais {len(itens) - 15} menções (veja o PDF)._")
    return titulo, "\n".join(linhas), ("nome",) if nomes else ()


# ------------------------------------------------------------------- main
def main():
    termos = carregar_termos()
    estado = carregar_estado()
    vistas = set(estado["processadas"])
    PASTA_PDF.mkdir(exist_ok=True)
    PASTA_GUARDADOS.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True, locale="pt-BR")
        page = context.new_page()

        try:
            edicoes = listar_edicoes(page)
        except Exception as e:  # noqa: BLE001
            edicoes, erro = [], e
        else:
            erro = None

        if not edicoes:
            PASTA_DEBUG.mkdir(exist_ok=True)
            try:
                page.screenshot(path=str(PASTA_DEBUG / "portal.png"), full_page=True)
                (PASTA_DEBUG / "portal.html").write_text(page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            print("Nenhuma edição encontrada na página.", erro or "")
            sys.exit(1)  # falha → o GitHub avisa por e-mail; os arquivos de debug ficam no artefato

        novas = [e for e in edicoes if e["chave"] not in vistas][:MAX_EDICOES]
        print(f"{len(edicoes)} edições listadas; {len(novas)} novas.")

        for ed in reversed(novas):  # da mais antiga para a mais nova
            nome_arq = f"DOE_{ed['data'].replace('/', '-')}_{abs(hash(ed['chave'])) % 10_000:04d}.pdf"
            destino = PASTA_PDF / nome_arq
            try:
                url = baixar_pdf(page, context, ed, destino)
                paginas = paginas_do_pdf(destino)
            except Exception as e:  # noqa: BLE001
                abrir_issue(f"⚠️ DOE {ed['data']}: falha ao baixar/ler", f"Erro: `{e}`", ("erro",))
                continue

            num = numero_edicao(paginas)
            achados = procurar(paginas, termos)
            titulo, corpo, rotulos = montar_relatorio(ed, num, url, len(paginas), achados)
            print(titulo)

            if any(a[0] == "NOME" for a in achados):
                (PASTA_GUARDADOS / nome_arq).write_bytes(destino.read_bytes())
            if achados or os.getenv("ALERTA_SEM_MENCAO") == "1":
                abrir_issue(titulo, corpo, rotulos)

            estado["processadas"].append(ed["chave"])
            salvar_estado(estado)

        browser.close()
    salvar_estado(estado)


if __name__ == "__main__":
    main()

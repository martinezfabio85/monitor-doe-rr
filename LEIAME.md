# Monitor do DOE-RR

Roda sozinho no GitHub de segunda a sexta, às 8h, 13h e 20h (horário de Boa Vista). Em cada execução, ele:

1. abre o portal da Imprensa Oficial (imprensaoficial.rr.gov.br) num navegador de verdade;
2. baixa cada edição nova do Diário Oficial;
3. procura os termos de `termos.txt` (o seu nome, o da Jardiane e a SEPLAN);
4. abre uma **Issue** no repositório com página e trecho de cada menção. O GitHub avisa você por e-mail e pelo app GitHub no celular.

| Ícone | Significado |
|---|---|
| 🔴 | Seu nome ou o da Jardiane apareceu. O PDF fica salvo na pasta `doe/` do repositório |
| 🟡 | Só a SEPLAN apareceu |
| ⚠️ | Falha ao baixar ou ler uma edição |

Todas as edições baixadas ficam disponíveis por 30 dias em **Actions → (execução) → Artifacts**.

## Instalação (uma vez só)

1. Crie um repositório **privado** no GitHub, por exemplo `monitor-doe-rr`.
2. Envie todos os arquivos desta pasta, incluindo a pasta oculta `.github/`.
3. Abra **Actions → Monitor DOE-RR → Run workflow** para rodar a primeira vez. Na primeira execução ele processa as 5 edições mais recentes.
4. No app GitHub do celular, confira se as notificações de Issues estão ativas.

## Ajustes

- **Termos:** edite `termos.txt`, uma linha por termo no formato `CATEGORIA | termo`.
- **Horários:** ficam no `cron` de `.github/workflows/monitor-doe.yml`, em UTC (Boa Vista = UTC+4 no cron).
- **Edições sem menção:** para receber Issue mesmo assim, use Run workflow com `alerta_sem_mencao = 1`.

## Se der erro

Se o site mudar de layout, a execução fica vermelha e o GitHub manda um e-mail. A captura de tela e o HTML da página ficam no artefato `debug/`. Mande esses arquivos para o Claude ajustar o script.

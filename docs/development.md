# Desenvolvimento

Use Linux ou WSL2 com Python 3.12, uv 0.8.0, Git e Make. Docker Engine com
Compose v2 permite validar Compose e executar os serviços reais. Restic e
ShellCheck participam dos testes de backup e lint. O alvo de produção continua
sendo Ubuntu Server 24.04 LTS amd64 com systemd; testes no Windows não comprovam
permissões, montagem, GPU, rede ou reprodução no servidor.

## Preparar um ambiente local

Na raiz do checkout:

```bash
python3.12 --version
uv --version
uv sync --frozen
uv run --frozen scripts/homeserver env init --env-file .env.dev --mode dev
uv run --frozen scripts/homeserver config validate --env-file .env.dev --mode dev
uv run --frozen scripts/homeserver config show --redacted --env-file .env.dev --mode dev
```

Init cria um arquivo privado e recusa sobrescrever um existente. O ambiente
gerado usa roots em `.runtime/dev`, binds loopback e credenciais locais. Não
aponte fixtures para `/srv/data`, appdata ou backups de produção. O loader lê
`.env` como dados, sem `source`, `eval` ou expansão de shell. Consulte
[a gramática e o catálogo](configuration.md).

## Executar os checks

```bash
uv run --frozen make lint test-unit test-contract test-integration compose-check smoke
uv run --frozen make test-config test-install test-restic
```

| Check | O que verifica |
|---|---|
| `lint` | Ruff, compilação Python, sintaxe Bash e ShellCheck quando instalado |
| `test-unit` / `test-contract` | Lógica e contratos HTTP com fixtures locais |
| `test-integration` | SQLite, filesystem temporário, recuperação e integrações disponíveis |
| `compose-check` | Render dev com Docker Compose, sem credenciais de produção |
| `smoke` | Fixtures de saúde, guarda de montagem, layout e scripts |
| `test-config` / `test-install` | Schema, configuração nativa, instalação e ciclo de release |
| `test-restic` | Captura, cópia entre repositórios e restore com Restic temporário real |

Os contratos do monitor qBit executam um Caddy real, compatível com a versão
2.10.2 usada pelo projeto. Instale o binário Linux no PATH ou informe seu caminho:

```bash
HOMESERVER_TEST_CADDY=/caminho/absoluto/caddy uv run --frozen make test-contract
restic version
uv run --frozen make test-restic
```

Sem Caddy, os testes do proxy são ignorados; sem Linux/Restic, o ciclo real de
backup também pode ser ignorado. Confira os skips antes de registrar aceite.
Os testes não baixam Caddy. A [CI](../.github/workflows/ci.yml) extrai o binário
da imagem do projeto e instala Restic/ShellCheck antes dos checks.

## Primeiro boot com APIs nativas reais

Compile as duas imagens locais antes do ensaio CPU:

```bash
docker build -f deploy/Dockerfile.control -t homeserver-control:dev .
docker build -f deploy/Dockerfile.telemetry -t homeserver-telemetry:dev .
uv run --frozen make test-fresh-stack
```

O validador cria um projeto isolado e um diretório privado novo em
`.runtime/fresh-*`, com portas loopback aleatórias, indexadores vazios e
providers desabilitados. Verifica os sete aplicativos nativos, reaplicação
sem diff, alteração/read-back qBit e prontidão; não adiciona torrents nem
importa estado existente. Remove somente containers/redes desse projeto e
conserva os arquivos privados da fixture. Docker indisponível retorna exit 3.
`scripts/validate-fresh-stack.py --help` descreve `--root` e `--timeout`.

Esse ensaio comprova o fluxo CPU isolado. Adopt de produção, reboot com disco
ausente/presente, acesso Tailscale, Intel UHD, SFTP e reprodução precisam de
evidência própria no ambiente correspondente.

## Alterar configuração ou comportamento

Declare cada chave em `homeserver_common.settings.FIELDS`, com tipo, unidade,
consumidor e modo de aplicação; conecte seu consumidor antes de documentá-la.
`uv run --frozen scripts/homeserver config catalog` e `config catalog --example`
geram as referências sem credenciais. Preferências nativas precisam de plan,
apply e leitura de confirmação. Testes de efeitos externos devem cobrir falha,
retomada e idempotência quando esse comportamento mudar.

Não adicione `.env`, tokens, bancos, mídia ou inventário bruto às fixtures ou
ao Git. Siga [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md)
e o [fluxo de releases](runbooks/releases.md) para revisão e publicação.

# Desenvolvimento e contribuições

Use Ubuntu 24.04 ou WSL2, Python 3.12, uv 0.8.0 e Docker Compose. O projeto
não concede uma licença de redistribuição; a escolha de licença cabe ao
proprietário. Dependências e imagens mantêm suas próprias licenças.

```bash
git clone https://github.com/reisguilherme/homelab-mediaserver.git
cd homelab-mediaserver
uv sync --frozen
uv run scripts/homeserver env init --env-file .env.dev --mode dev
uv run make lint test-unit test-contract test-integration smoke
uv run make compose-check test-config test-install test-restic
```

As verificações de instalação e restauração usam fixtures locais, SQLite e
repositórios Restic temporários. O teste de primeiro boot exige Docker e
imagens CPU compiladas; consulte os targets do Makefile e a CI.

Faça alterações numa branch, descreva o comportamento resultante e registre
as verificações relevantes no pull request. Testes de política precisam cobrir
falhas, reinícios e efeitos externos quando estes mudarem. Fixtures nunca
devem carregar bancos ou mídia de produção. Operações de filesystem, rede,
GPU e reprodução precisam de validação Linux ou no hardware correspondente.

O catálogo e `.env.example` vêm de `homeserver_common.settings.FIELDS`.
Ao adicionar configuração, declare tipo, unidade, consumidor e aplicação;
conecte seu consumidor e regenere o exemplo. Mudança de preferência nativa
precisa de plan, apply e leitura de confirmação.

Prefira os contratos existentes para qualidade, idioma e importação. Áudio de
release só recebe preferência com metadados declarados; títulos não comprovam
idioma. Sonarr/Radarr devem manter importação automática desabilitada e
hardlinks habilitados. Alterar essas guardas muda ownership e capacidade do
fluxo; consulte [configuração](docs/configuration.md) e
[arquitetura](docs/architecture.md) antes de modificá-las.

Nunca publique `.env`, credenciais, bancos, mídia ou inventário bruto. Use
`config show --redacted` e siga [SECURITY.md](SECURITY.md) para vulnerabilidades.
Não execute `.env` como shell. Builds e releases de produção usam checkout
limpo, SHA completa, imagem por digest e deploy manual.
Registre ambiente, comando e resultado real no
[aceite](docs/evidence/productization-acceptance.md). Testes ignorados, fixtures
e primeiro boot em containers não comprovam reboot físico ou uma VM vazia.

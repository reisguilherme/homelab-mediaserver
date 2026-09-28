# Desenvolvimento

O projeto é pessoal. Use Linux/WSL2, Python 3.12, uv e Docker Compose.
A licença de redistribuição ainda não foi escolhida pelo proprietário.

```bash
uv sync --frozen
uv run make lint test-unit test-contract test-integration smoke
uv run make compose-check
```

O [guia de desenvolvimento](docs/development.md) detalha dependências dos checks.
Testes usam fixtures locais e diretórios temporários. Não copie bancos, mídia
ou credenciais do servidor para o checkout.

Configuração editável pertence à lista de parâmetros/credenciais do `.env`.
Portas, paths, URLs internas e demais defaults técnicos pertencem ao código
ou Compose. Não criar outra camada de deploy, release, backup ou CI/CD.

Mudanças de política devem preservar qualidade, importação sequencial de séries, capacidade
real, importação e exclusão explícita. Cubra efeitos externos e falhas relevantes
quando esse comportamento mudar. Registre o comando e resultado realmente obtidos.

Nunca publique `.env`, tokens, dumps ou inventário bruto. Consulte
[SECURITY.md](SECURITY.md) e a [arquitetura](docs/architecture.md).

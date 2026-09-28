# Desenvolvimento

Use Linux/WSL2, Python 3.12 e uv. Para executar a aplicação basta Docker Compose;
as ferramentas Python no host são necessárias para desenvolvimento.

```bash
uv sync --frozen
uv run make lint
uv run make test-unit test-contract test-integration smoke
uv run make compose-check
```

Os testes usam fixtures locais, SQLite e filesystem temporário. Nunca use
`/srv/data`, appdata ou bancos reais como fixtures. Docker precisa estar
disponível para validar o Compose; ausência de Docker não comprova esse check.

Os contratos do monitor qBit usam um Caddy real. Informe o binário Linux:

```bash
HOMESERVER_TEST_CADDY=/caminho/absoluto/caddy uv run make test-contract
```

Confira skips e dependências antes de afirmar que um ensaio passou.
Testes locais não comprovam rede, Intel UHD ou reprodução na TV.

A configuração pública vem do schema de parâmetros editáveis e credenciais.
Ao adicionar uma chave, conecte seu consumidor e atualize o exemplo/catálogo.
Defaults técnicos ficam no código/Compose. Evite introduzir instaladores,
gerenciadores de release/backup ou serviços auxiliares de implantação.

Scanners podem ser executados manualmente durante revisão:

```bash
bash scripts/check-secrets.sh
bash scripts/check-images.sh --output-dir .runtime/image-security homeserver-control:dev homeserver-telemetry:dev
```

Use referências explícitas de imagens construídas; não passe inventário de
containers ou diretórios privados. Consulte [segurança de imagens](image-security.md)
e [CONTRIBUTING.md](../CONTRIBUTING.md).

Resultados e limites da verificação atual estão no
[aceite da stack pessoal](evidence/personal-compose-acceptance.md).

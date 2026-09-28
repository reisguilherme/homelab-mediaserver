# Segurança

Reporte problemas de segurança pelo recurso privado de security advisories do
[repositório](https://github.com/reisguilherme/homelab-mediaserver/security/advisories).
Não publique senhas, arquivos `.env`, bancos nativos, inventários ou URLs com tokens
em issues, logs ou anexos.

Os painéis são destinados à rede local e ao Tailscale. A API nativa do qBittorrent
fica numa rede Docker interna; o painel de administração usa autenticação nativa
e um proxy vinculado ao endereço Tailscale. Downloads automáticos passam pelo
gateway, que verifica identidade, metadados e espaço disponível.

`scripts/homeserver env init` gera credenciais locais. A configuração efetiva fica
no `.env` do operador, com permissão `0600`, fora do checkout publicado. Use
`config show --redacted` para diagnósticos. Arquivos `_FILE` são lidos como dados;
os scripts não executam o conteúdo do `.env`.

Se um segredo for publicado, revogue-o no serviço correspondente e atualize o
`.env`. Apagar o arquivo do commit mais recente não remove cópias do histórico.
O pipeline verifica segredos com Gitleaks antes de produzir uma release.

Backups Restic têm senha própria. A cópia externa deve ser outro repositório
Restic, com retenção independente. A restauração de validação usa um diretório
isolado; ela não sobrescreve mídia ou bancos em operação.

As configurações adotadas de uma instalação existente preservam seus usuários,
bibliotecas e credenciais. A alteração desses dados deve ser feita explicitamente
pelo operador. Consulte [o guia do operador](docs/operator-guide.md).

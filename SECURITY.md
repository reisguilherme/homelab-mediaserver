# Segurança

Credenciais ficam diretamente no `.env` local, com permissão `0600`, fora do Git.
Não use senhas reais nos exemplos, fixtures, screenshots, issues ou logs.
O `.gitignore` e o contexto Docker excluem estado privado; confira isso antes
de publicar alterações.

Os painéis são usados na rede doméstica ou pelo Tailscale. A API nativa do
qBittorrent fica interna ao Docker; o monitor passa por proxy somente leitura
e mantém login nativo. Downloads passam pelo gateway com capacidade real.

Se um segredo for publicado, revogue-o no serviço e troque o valor no `.env`.
Excluir o arquivo do último commit não remove cópias antigas. Diagnósticos de
configuração devem usar saída redigida.

O projeto não abre portas do roteador nem apaga mídia por ela ter sido assistida.
A exclusão é uma ação explícita coordenada entre serviços.

Reporte vulnerabilidades pelo recurso privado de
[security advisories](https://github.com/reisguilherme/homelab-mediaserver/security/advisories).
Os [checks locais](docs/development.md) incluem scanners opcionais para revisão
de código e imagens; não são requisito de um pipeline de implantação.

# Preparação dos serviços

O Compose base não publica portas e não contém caminhos do host. Use
`compose.dev.yaml` para fixtures/loopback e `compose.prod.yaml` somente depois
da auditoria, guarda de UUID e criação explícita de `/etc/homeserver/server.env`.

Antes da primeira aquisição, configurar uma instância de Sonarr e Radarr,
desabilitar busca/RSS/grabs autônomos e apontar os Arr apenas para o gateway.
O qBittorrent permanece na rede `transfer`; não publicar sua API na LAN.

Validar a versão real das imagens e preencher digests no manifesto de release.
Tags do arquivo `config/versions.env` são referências de desenvolvimento, não
prova de compatibilidade ou de segurança da produção.

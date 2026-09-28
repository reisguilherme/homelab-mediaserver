# Histórico — migração da produtização

O guia anterior de importação de roots, UUID, units, secret files, backups e
manifests foi substituído em 28/09/2026 pela instalação pessoal com Compose.
A versão integral está no
[histórico Git](https://github.com/reisguilherme/homelab-mediaserver/blob/89c84f26e4426d2f025573ce3728c5fa320f6267/docs/migrations/2026-09-productization.md).

A orientação atual está em
[reutilizar uma instalação](../installation.md#reutilizar-uma-instalação).
Preserve bancos/mídia, importe credenciais reais diretamente no `.env`,
adapte paths ao layout fixo e não execute duas stacks sobre os mesmos dados.
O operator preserva IDs e não redefine contas existentes silenciosamente.

Os defaults técnicos não devem ser copiados para o `.env`. Prefira o
[exemplo atual](../../.env.example) e o [catálogo de configuração](../configuration.md).

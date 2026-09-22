# Auditoria da base do servidor

`audit-server.sh` é somente leitura. Ele registra o estado observado do Ubuntu, Docker, montagem de mídia, energia, serviços, permissões e dispositivos de vídeo sem imprimir chaves, tokens, cookies ou conteúdo de `authorized_keys`.

## Preparação

1. Copie `config/server.example.yaml` para um arquivo local fora do Git e preencha o UUID obtido por `findmnt --mountpoint /srv/data`.
2. Confirme que o alias `homeserver` do cliente SSH aponta para o Legion. O alias é uma configuração local; o script não presume hostname ou IP.
3. Execute primeiro uma sessão LAN e depois uma sessão Tailscale SSH. Registre apenas resultado, identidade e horário no relatório sanitizado.

## Execução

```bash
AUDIT_INVENTORY_PATH=config/server.local.yaml \
AUDIT_REPORT_PATH=docs/evidence/server-audit.md \
bash scripts/audit-server.sh --check
```

Para integração automatizada:

```bash
bash scripts/audit-server.sh --check --json > .runtime/server-audit.json
```

O código de saída é `0` quando todas as sondagens obrigatórias retornam sucesso, `2` quando o inventário foi obtido mas há diferenças/ausências, e `1` quando uma falha impede a decisão. A auditoria não monta discos, altera `fstab`, reinicia serviços, modifica energia, concede privilégios ou autentica o Tailscale.

## Guarda da montagem

Antes de iniciar a stack ou admitir um download, valide a identidade física e o modo de escrita:

```bash
bash scripts/check-mount.sh /srv/data REPLACE_WITH_FILESYSTEM_UUID
```

O caminho precisa ser exatamente um mountpoint, o UUID precisa coincidir, a montagem não pode conter `ro` e o processo precisa ter permissão de escrita. A existência do diretório isolado em `/` não satisfaz a guarda.

## Evidência

Relatórios compartilháveis devem conter somente `docs/evidence/` com valores sanitizados. Inventário bruto, configurações locais, saída de rede e segredos ficam fora do Git. O status `reported` de uma informação fornecida pelo usuário não equivale a `verified` por este auditor.

## Pendências dependentes do host

- UUID, filesystem, capacidade, UID/GID e grupos `render`/`video`.
- Render node Intel e driver QSV/VA-API.
- Regras de rede e regras SSH da tailnet, verificadas separadamente.
- Acesso LAN e Tailscale com IPv4 e IPv6.
- Hardlinks dentro dos containers usando as identidades efetivas das imagens.

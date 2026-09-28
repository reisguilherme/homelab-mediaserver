# Diagnóstico da base do servidor

O diagnóstico atual lê o `.env` canônico e verifica dependências/montagem sem
alterar o host:

```bash
sudo .venv/bin/python scripts/homeserver doctor --env-file /etc/homeserver/.env
bash scripts/check-mount.sh /CAMINHO_DA_MIDIA UUID_VERIFICADO
```

O mountpoint deve ser exato, ter UUID correto e escrita permitida. Diretório
existente no filesystem raiz não comprova o disco de mídia. Paths, UID/GID e
Intel opcional devem corresponder ao hardware real da máquina.

`scripts/audit-server.sh` e `config/server.example.yaml` permanecem como
instrumentos históricos de inventário somente leitura. Não são configuração
ativa e não substituem Settings/install/doctor; seus relatórios físicos exigem
revisão sanitizada antes de compartilhamento.

SSH LAN, autorização da tailnet, hardlinks, Intel e reprodução exigem ambiente
real. Registre reported/verified/missing/unknown; não transforme declaração do
usuário ou fixture em prova. Inventário bruto, tokens e chaves ficam fora do Git.
O diagnóstico não altera rede, fstab, Lenovo, GPU ou portas do roteador.

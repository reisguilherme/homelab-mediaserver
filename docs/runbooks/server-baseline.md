# Conferir o host

Estes comandos consultam o servidor sem alterar rede ou armazenamento:

```bash
uname -a
ip -br addr
ip route
df -hT /srv/data /srv/appdata /srv/transcode
findmnt --target /srv/data
docker version
docker compose version
docker compose ps
tailscale status
tailscale ip -4
```

Confirme que `/srv/data` está no filesystem pretendido e tem espaço/escrita
para UID/GID 1000. Um diretório existente não comprova um disco separado.
Downloads e biblioteca devem compartilhar filesystem para usar hardlinks.

Compare resultados com [instalação](../installation.md) e
[diagnóstico](../troubleshooting.md). SSH, acesso Tailscale, GPU e reprodução
exigem verificação na máquina real. Registre declaração e medição separadamente;
não publique IPs privados, tokens ou inventário bruto no Git.

O projeto não altera Netplan, roteador, `fstab`, partições ou energia do host.

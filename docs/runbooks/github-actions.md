# GitHub Actions e acesso privado

CI executa em push/PR sem credenciais de produção: lockfile, lint, contratos,
Restic real, imagens CPU, primeiro bootstrap com APIs nativas e scanners.
Os workflows de release e deploy são manuais e exigem CI aprovada em `main`.

Execute **Build HomeServer release** com `commit` igual à SHA completa testada.
Ele publica imagens próprias no GHCR e um artefato com digests/checksums.
Depois execute **Deploy HomeServer** com o mesmo `commit` e `release_run` igual
ao ID do build aprovado. O deploy autentica a origem e identidade do artefato;
a API não recebe URLs arbitrárias. O mesmo `scripts/deploy.sh` é usado localmente.

Secrets, variáveis, autorização da tailnet e sudo estão descritos no
[guia de instalação](../installation.md#deploy-manual-pelo-github-actions).
O runner usa identidade Tailscale efêmera e host key SSH previamente validada.
Não abrir portas do roteador nem conceder sudo irrestrito como atalho.

Jobs são serializados por concurrency. Falha de transporte impede ativação;
falha na release mantém recuperação/manutenção conforme o contrato local.
Relatórios públicos contêm somente código, identidade de imagem e achados dos
scanners. `.env`, bancos, tokens, backups e inventário vivo ficam no servidor.

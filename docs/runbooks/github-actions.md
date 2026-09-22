# GitHub Actions e acesso privado

CI roda em push e pull request sem segredos de produção. Deploy é somente `workflow_dispatch`, serializado por `concurrency`, e recebe um commit já validado. O executor hospedado entra no Tailscale com identidade efêmera e usa o usuário/host SSH dedicados configurados nos secrets.

As permissões da workflow são somente leitura. A regra de rede da tailnet e a regra de autorização Tailscale SSH são verificadas separadamente; `authorized_keys` do OpenSSH LAN não é tratado como prova de autorização Tailscale. O usuário do pipeline não recebe grupo `docker` ou sudo irrestrito automaticamente.

Antes de habilitar a workflow, revisar a SHA completa da action Tailscale, validar a regra SSH não interativa e provar a transferência com `deploy.sh` manual. A referência atual é `v4.1.3` (`780049a30b6ff5c378a9e7b389d15ece7a204888`). Falha de rede mantém a execução como falha e não avança o apontador de release.

O workflow de produção é manual e exige `commit`, `artifact` e `manifest`. Ele valida um SHA hexadecimal em minúsculas, confirma que o checkout corresponde ao SHA e consulta a API do GitHub para encontrar uma execução concluída e bem-sucedida de `HomeServer CI` para esse commit em `main`. Artefatos podem ser caminhos dentro do workspace ou URLs HTTPS; outros esquemas e caminhos fora do workspace são recusados. O mesmo manifesto é transferido com o artefato e passado a `scripts/deploy.sh --manifest`, que faz a validação final no host. Os arquivos temporários no host são removidos após a tentativa de implantação.

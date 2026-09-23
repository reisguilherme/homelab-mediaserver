# Painel web de status

Abra `http://<IP_TAILSCALE_DO_SERVIDOR>:8081/ui/status` em um dispositivo conectado ao mesmo tailnet. O painel é somente leitura. A porta 8081 é publicada apenas no endereço Tailscale configurado em `TAILSCALE_BIND_IP`; não é necessário abrir porta no roteador.

O navegador atualiza a rota `/api/v1/status` a cada cinco segundos. O timer do host produz `/run/homeserver/host.json` e `/run/homeserver/capacity.json` a cada 15 segundos. A primeira amostra de CPU e rede ainda não tem taxa; esses valores aparecem como indisponíveis até a amostra seguinte. Métricas antigas são identificadas no painel, sem convertê-las em zero.

O serviço de telemetria mede em segundo plano, no máximo uma vez a cada cinco minutos, os blocos ocupados por `/data/media/movies`, `/data/media/tv` e `/data/torrents`. Um arquivo com hardlink presente em mais de uma pasta conta uma vez, com prioridade para filmes, depois séries e por último torrents. **Outros** é o espaço usado no volume que não pertence às três categorias, incluindo outras pastas e metadados do sistema de arquivos. Durante o primeiro levantamento, a divisão aparece como “Calculando pastas” e o restante do painel permanece disponível.

O serviço precisa dos binds somente leitura `/run/homeserver:/run/homeserver` e `/srv/data:/data`. A página não recebe credenciais, não consulta Docker e não oferece ações de controle. Se o host, a capacidade ou o volume estiverem indisponíveis, a respectiva seção mostra o estado sem inventar uma leitura.

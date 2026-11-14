# HomeServer — plano de correções e melhorias de segurança

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans para executar as mudanças sequenciais de infraestrutura; use superpowers:subagent-driven-development apenas para tarefas de código independentes, com revisão antes da integração. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** corrigir os riscos confirmados do acesso público e reduzir o alcance de um comprometimento, preservando os fluxos existentes e a recuperação operacional.

**Architecture:** manter inicialmente a TV box como entrada HTTPS de Jellyfin/Seerr; aplicar filtro de rede, atualizar aplicativos, separar o relay público do controlador e restringir integrações. LAN/Tailscale permanecem privados. Segmentação física/VLAN é uma entrega separada para conter comprometimento root.

**Tech Stack:** Docker Compose, Python 3.12/FastAPI/httpx, Nginx, Caddy/Dynu, Jellyfin, Seerr, firewall Linux e Tailscale.

**Spec:** [desenho e requisitos](../specs/2026-10-15-public-access-security-design.md).

**Estado:** plano desenvolvido; implementação e implantação não iniciadas. A revisão deste documento precede qualquer mutação em produção. Execução recomendada: nesta sessão, sequencial para servidores, com revisão independente do código e dos testes de fronteira.

## Global Constraints

- Desenvolvimento e testes de referência: Linux/WSL2, Python 3.12, `uv sync --frozen`.
- Nenhuma formatação, alteração de montagem/fstab, ativação RTX ou mudança no serviço Lenovo.
- Nenhuma abertura de porta no roteador pelo agente; qBit administrativo nativo permanece sem exposição pública.
- Nenhuma exclusão de mídia, alteração da fila ou download como efeito colateral dos testes.
- Jellyfin mantém escrita para edições e legendas nesta etapa; o risco de escrita de mídia permanece explícito.
- Preservar `compose.yaml`, `compose.storage.yaml`, `compose.storage.extra.yaml` e `compose.storage.fourth.yaml` na avaliação da configuração efetiva.
- Nenhum CI/CD, gerenciador de backups, release própria ou unit de host nova.
- Testes usam fixtures temporárias; ensaios de migração usam cópias privadas isoladas, nunca bancos ativos compartilhados.
- `.env`, credenciais, bancos, inventário bruto e endereços privados reais ficam fora do Git.
- Mudanças existentes no workspace não serão revertidas, limpas ou incluídas indiscriminadamente em commits.

## Review Focus

1. **Troca de IP/reinício:** confiança de proxy e bloqueios continuam corretos depois de recriar containers, trocar IP e reiniciar hosts — T06/T08/T11.
2. **Sessão administrativa antiga:** token já emitido não contorna a restrição pública, inclusive por relay, WebSocket ou exclusão coordenada — T07/T09/T11.
3. **Migração parcialmente concluída:** reversão restaura configuração/banco/imagem do mesmo checkpoint; nenhum aplicativo antigo abre banco migrado — T01/T04.
4. **Origem forjada/IPv6/DNS:** headers, aliases DNS, destino IPv6 global local e portas publicadas não abrem caminho administrativo — T02/T06/T08/T11.
5. **Compatibilidade de uso real:** HTTPS privado, CSRF, legendas, seek, transcode, importação e pedidos funcionam com os controles ligados — T03/T04/T05/T09/T10/T11.

## Sequência e dependências

| Tarefa | Prioridade | Entrega verificável | Dependências |
| --- | --- | --- | --- |
| T01 | P0 | Estado inicial, acesso de recuperação e checkpoints | Aprovação de execução |
| T02 | P0 | TV box: entrada filtrada, SSH e Caddy endurecidos | T01 |
| T03 | P0 | Clientes Python compatíveis com autenticação Jellyfin atual | T01; desenvolvimento paralelo a T02 |
| T04 | P0 | Jellyfin/Seerr corrigidos e bancos migrados | T03; ensaio antes de produção |
| T05 | P0 | Logs sem tokens, diagnóstico preservado | T01; desenvolvimento independente |
| T06 | P1 | Relay e ambientes por papel sem segredos excessivos | T03/T04/T05 |
| T07 | P1 | Integrações HTTP mínimas e exclusão pública bloqueada | T06 |
| T08 | P1 | Redes Docker e firewall HomeServer com negativas reais | T06/T07 |
| T09 | P1 | HTTPS privado, CSRF e políticas de contas persistentes | T04/T06/T08 |
| T10 | P1 | Limites medidos e privilégios mínimos | T04/T08 |
| T11 | P0/P1 | Aceite integrado, persistência e retomada pública | T02–T10 |
| T12 | P2 | Projeto executivo de segmentação externa e evolução de mídia | Pode ser estudado durante T03–T10; não bloqueia os patches |

P0 significa correção prioritária; P1 é necessário para aprovar a **Entrega A** da especificação. T12 é necessário para afirmar a **Entrega B**, isolamento perante comprometimento de host. Não agrupar tudo em um único restart da stack.

## Mapa de arquivos e responsabilidades

| Arquivo / conjunto | Mudança planejada |
| --- | --- |
| `services/common/src/homeserver_common/render.py` | Fonte das imagens, redes, ambientes por papel, hardening e limites |
| `services/common/src/homeserver_common/settings.py`, `.env.example`, `config/dev.env.example` | Parâmetros de segurança explícitos e documentação, sem segredos reais |
| `services/common/src/homeserver_common/runtime.py` | Parar distribuição automática de todas as credenciais aos processos |
| Novo `services/common/src/homeserver_common/service_environment.py` | Allowlist de ambiente por papel; credencial nova negada por padrão |
| Novo `services/common/src/homeserver_common/jellyfin_auth.py` | Header moderno de autenticação das chamadas internas |
| `services/control/src/homeserver_control/configuration/media_servers.py` e novo `configuration/security.py` | Reconciliação de versões/políticas, com plan/apply/verify e readback |
| `services/control/src/homeserver_control/api/app.py`, `api/jellyfin_labels.py`, novo `api/jellyfin_relay.py` | Separar o serviço público de apresentação do controlador |
| Novo `services/control/src/homeserver_control/api/public_access.py` | Verificação adicional de identidade pública via Jellyfin, sem armazenar senha/token ou substituir autorização nativa |
| `services/control/src/homeserver_control/api/deletion_capture.py` | Manter autorização privada e tratar origem validada de maneira explícita |
| `deploy/jellyfin-proxy/nginx.conf` | Roteamento, confiança de proxy, logs e bloqueio de exclusão na entrada pública |
| `deploy/service-proxy/Caddyfile` | Entrada privada e sanitização de headers |
| Novos `deploy/public-ingress/Caddyfile.example`, `caddy-hardening.conf.example` | Templates sem credenciais/IPs reais para a borda existente |
| Novo `deploy/seerr-proxy/nginx.conf` | Única entrada do backend Seerr, headers validados e healthcheck interno |
| Novos `services/control/src/homeserver_control/api/automation_proxy.py`, `api/integration_policy.py` | Serviço mínimo com upstreams fixos e validação de métodos/rotas/corpos Seerr → *arr |
| `compose.yaml` e três overlays de armazenamento | Regenerar/validar a configuração efetiva sem perder volumes |
| Novos `docs/runbooks/public-access-security.md`, `docs/runbooks/public-access-recovery.md` | Operação, firewall, testes externos e recuperação pontual |
| `SECURITY.md`, `docs/configuration.md`, `docs/operator-guide.md` | Atualizar o modelo de exposição e os procedimentos do proprietário |

Os templates de borda não são instaladores e não criam units. Arquivos aplicados no host permanecem sob gestão nativa existente. Novos nomes de arquivos/testes abaixo são entregáveis, não arquivos que já existem.

## T01 — Preparação, acesso e reversão

**Interfaces:** consome auditoria e configuração atual; produz `.runtime/security-hardening-20261015/baseline.json`, `rollback.md` e checkpoints privados consistentes. Sem credenciais na saída do terminal.

**Arquivos:** criar os dois runbooks da tabela; evidências operacionais somente em `.runtime/security-hardening-20261015/`.

- [ ] Confirmar que o working tree contém alterações anteriores; registrar diff/path relevantes, sem reset/clean. Uma eventual worktree de implementação precisa conter o estado aprovado atual, inclusive arquivos ainda não versionados; partir apenas de HEAD perderia correções existentes.
- [ ] Confirmar sudo administrativo no HomeServer, ainda não comprovado na auditoria, e chave SSH funcional em **duas sessões novas**, LAN e Tailscale. A sessão existente não conta como prova de novo acesso.
- [ ] Coletar backend firewall, regras, rotas IPv4/IPv6, ACL/grants Tailscale, listeners, uso de recursos, plugins, imagens/digests e versões. Usar consultas redigidas; não despejar `docker inspect`/Compose completo com ambiente no chat.

```bash
sudo -n true
docker info --format '{{.ServerVersion}}'
sudo iptables -V
sudo iptables-save
sudo ip6tables-save
sudo nft list ruleset
ip -json address
ip -json route show table all
ip -6 -json route show table all
tailscale serve status --json
tailscale funnel status --json
```

Comandos ausentes são registrados como ausentes; não instalar/trocar backend por inferência. Salvar regras/inventário em arquivos privados. Inspecionar a configuração efetiva do daemon para distinguir `iptables-nft` de backend Docker `nftables`.

- [ ] Registrar mounts/UUIDs e Compose efetivo dos quatro arquivos. Comparar presença de cada volume antes/depois sem modificar montagem.
- [ ] Antes da janela de migração, colocar **somente a entrada pública** em manutenção; preservar entradas privadas. Interromper temporariamente tarefas que escrevem nos bancos a serem copiados. qBit permanece operando.
- [ ] Parar o aplicativo correspondente e copiar seu appdata inteiro de forma consistente, preservando permissões. Conferir espaço e hashes antes de iniciar versão nova. Cópia SQLite em uso sem mecanismo consistente não é aceita.
- [ ] Ensaiar restauração em diretório temporário com UID/GID corretos. Registrar imagem + appdata + configuração como conjunto indivisível de rollback.
- [ ] Preparar reversão temporizada das alterações de rede, independente da conexão SSH, usando recurso já disponível da distribuição. Testar disparo/cancelamento em regras de teste. Não criar unit persistente; sem mecanismo validado e acesso de recuperação, não aplicar política restritiva remota.

**Aceite:** nova conexão administrativa funciona; checkpoint restaurável comprovado; nenhuma mídia/entrada da fila alterada. **Reversão:** só arquivos/cadeias da intervenção; não restaurar cegamente um dump que apague regras novas de Docker/Tailscale.

## T02 — TV box: firewall antes do TLS, SSH e Caddy

**Interfaces:** consome IPs/CIDRs privados e backend de T01; produz regras persistentes e drop-in da unit Caddy existente. Não modifica roteador.

**Arquivos:** templates `deploy/public-ingress/`; runbooks; arquivos do host `/etc/caddy/Caddyfile`, drop-in existente de Caddy e configuração nativa de SSH/firewall, copiados antes de editar.

- [ ] Criar política em fixture Linux com a mesma família de backend: aceitar loopback, conexões estabelecidas, administração autorizada, dependências de DHCP/Tailscale/ICMPv6 e TCP 443 somente da lista autorizada. A TV box recebe **443**, embora a WAN use **54443**.
- [ ] Negar demais entradas novas IPv4/IPv6. Manter DNS-01; não abrir 80/443 externa para ACME. Comprovar qual IP chega na TV box pelo encaminhamento/hairpin antes de usar a regra como proteção.
- [ ] Comprovar que a política é carregada **antes** da disponibilização dos listeners públicos no boot, usando dependências do mecanismo nativo existente e drop-in da unit do fornecedor quando necessário. Se o firewall não carregar, Caddy público não inicia. Um script que bloqueia portas somente depois do boot não satisfaz esse requisito.
- [ ] Restringir saída do UID Caddy: upstreams fixos nas duas portas, DNS conhecido e HTTPS necessário a ACME/Dynu. Negar demais destinos privados/on-link/tailnet em ambos protocolos. Aplicar inicialmente por processo, sem cortar OUTPUT geral do host.
- [ ] Preservar somente `CAP_NET_BIND_SERVICE` se o bind exigir; remover `CAP_NET_ADMIN`, aplicar `NoNewPrivileges`, `PrivateDevices`, `ProtectHome` e escrita somente nos diretórios Caddy necessários. Validar certificado, armazenamento ACME e reload antes de aplicar.

```ini
[Service]
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
PrivateDevices=yes
ProtectHome=yes
ProtectSystem=strict
```

Completar `ReadWritePaths` com os caminhos reais descobertos do Caddy; falhar se ausentes, sem liberar `/etc`, `/home` ou filesystem inteiro. Isso altera a unit do fornecedor por drop-in, não cria serviço do projeto.

- [ ] Depois de validar as chaves novas, configurar `PermitRootLogin no`, `PasswordAuthentication no` e `KbdInteractiveAuthentication no`, respeitando includes/Match. Executar `sshd -t`, consultar `sshd -T` para o usuário/origem real e fazer reload sem encerrar a sessão de recuperação.
- [ ] Conferir alcance da chave Dynu e reduzir ao mínimo suportado. Manter credenciais DNS e DDNS distintas. Rotacionar apenas depois de validar substituição; não registrar valores nem alterar a senha principal da conta por conveniência.
- [ ] Levantar atualizações de segurança Debian/Armbian e aplicar primeiro as compatíveis com a versão instalada. Kernel/bootloader exigem teste de recuperação física: não fazer `dist-upgrade` indiscriminado na TV box.

**Teste:** como UID Caddy, upstreams e renovação DNS funcionam; SSH/*arr/control/qBit/roteador não conectam. Origem externa não autorizada não completa TLS; em fixture HTTP, Caddy ainda retorna 403 se a camada de rede for atravessada. Duas novas sessões privadas continuam funcionando. **Rollback:** restaurar apenas drop-in/sshd/regras alteradas, mantendo publicação em manutenção quando a contenção falhar.

## T03 — Autenticação Jellyfin compatível com a versão corrigida

**Interfaces:** criar `jellyfin_headers(token: str) -> dict[str, str]` em `homeserver_common.jellyfin_auth`. Consome token interno já obtido; produz `Authorization: MediaBrowser ...` com identificação estável e rejeição de caracteres inválidos. Não aceita token de cliente para substituir por chave administrativa.

**Modificar:** `adapters/jellyfin.py`, `configuration/reconcile.py`, `configuration/media_servers.py`, `api/deletion_capture.py`, `worker/deletion_coordinator.py`, `worker/finalization.py`, `worker/jellyfin_collections.py`, `homeserver_common/usb_recovery.py`; procurar outros produtores com `rg`, não somente leitores de headers.

**Testes:** criar `tests/unit/test_jellyfin_auth.py`; ampliar contratos de exclusão, finalização, coleções, preferências e recuperação USB.

- [ ] Escrever teste antes da mudança e comprovar falha pela inexistência do helper:

```python
import pytest
from homeserver_common.jellyfin_auth import jellyfin_headers

def test_modern_auth_does_not_emit_legacy_token_header():
    headers = jellyfin_headers("synthetic-token")
    assert headers["Authorization"].startswith("MediaBrowser ")
    assert 'Token="synthetic-token"' in headers["Authorization"]
    assert "X-Emby-Token" not in headers

@pytest.mark.parametrize("token", ["", "x\r\ny", 'x"y', "x\\y"])
def test_invalid_token_fails_without_echoing_secret(token):
    with pytest.raises(ValueError, match="Invalid Jellyfin credential"):
        jellyfin_headers(token)
```

- [ ] Implementar o helper, substituir produtores legados e preservar identidade nos relays. Erros não incluem tokens. Não reativar autenticação legada como solução para testes.
- [ ] Rodar fixture Jellyfin com legado desativado: leitura autenticada, `/Users/Me`, biblioteca, coleções, refresh após importação e recuperação USB. Credenciais sintéticas não são persistidas em snapshots públicos.
- [ ] Testar clientes web/TV/mobile usados pelo proprietário, inclusive WebSocket e URLs de mídia. Query `ApiKey` e `api_key` não são equivalentes na versão nova; não fazer tradução global de parâmetros que gere credencial ambígua.

```bash
uv run pytest tests/unit/test_jellyfin_auth.py tests/contract/test_jellyfin_delete_capture.py tests/contract/test_jellyfin_label_proxy.py tests/unit/test_usb_recovery.py -q
```

**Aceite:** chamadas próprias funcionam com autenticação moderna e usuário comum não ganha privilégios. **Rollback:** código anterior somente com imagem antiga e checkpoint correspondente; nunca abrir legado publicamente como bypass de compatibilidade.

## T04 — Migração ensaiada de Jellyfin e Seerr

**Interfaces:** consome helper T03 e checkpoints T01; produz versões e digests fixos aprovados, relatório de integridade do catálogo e pedidos.

**Modificar:** `render.py::IMAGES`, `compose.yaml`, `media_servers.py` somente onde API nova exigir; contratos `test_native_configuration.py`, `test_stack_render.py`, `test_seerr_write_contract.py`.

- [ ] Resolver manifestos oficiais para Jellyfin 12.2 e Seerr 3.5.0 e conferir arquitetura/versão. Revalidar avisos de segurança na data da execução. Nginx e Caddy internos também passam por revisão de atualização, sem atribuir CVE de recurso não utilizado.

```bash
docker buildx imagetools inspect jellyfin/jellyfin:12.2
docker buildx imagetools inspect ghcr.io/seerr-team/seerr:v3.5.0
```

- [ ] Criar instâncias temporárias sem portas públicas, com cópias privadas dos appdata, saída bloqueada e integrações substituídas por fixtures. Nenhum job pode atingir Radarr/Sonarr/qBit reais. Mídia do ensaio é uma amostra temporária, não a biblioteca real.
- [ ] Conferir colisões de nomes de usuários por maiúsculas/minúsculas e compatibilidade dos plugins. Rodar migração Jellyfin sem interromper/reiniciar por timeout curto; validar `--mode MigrateSystem` na imagem escolhida.
- [ ] Ensaiar Seerr oficial mantendo `/app/config`; adicionar `init: true`, conferir UID 1000 e permissões. O código já tem POST de sincronização/PUT por biblioteca: provar que a versão nova não cai em fallback GET nem desativa bibliotecas durante leitura.
- [ ] Comparar IDs/contagens, progresso assistido, coleções, capas/títulos nacionais, pedidos, usuários, permissões e credenciais de integração **sem imprimir os valores**, antes do scan na cópia. Testar scan em um catálogo de amostra separado com resultado esperado: escanear um banco completo sem todos os arquivos pode remover registros da cópia e não é uma comparação válida de integridade da migração. Ajustar política de legendas por biblioteca quando necessário.
- [ ] Ensaiar rollback completo de cada app. Registrar tempo observado de migração e scan para dimensionar a janela, sem prometer duração antes do ensaio.
- [ ] Implantar um aplicativo por vez com os quatro arquivos Compose; recriar apenas serviços afetados. Avaliar pausas breves do controlador para impedir escrita concorrente; não parar qBit nem limpar fila.
- [ ] Fazer scan de produção somente após verificar os quatro volumes disponíveis; se algum mount estiver ausente, interromper a operação para não confundir indisponibilidade com remoção do catálogo.

**Aceite:** versões corrigidas, ausência de regressão relevante e checkpoints restauráveis. **Rollback:** parar novo app, restaurar appdata/config anterior inteiro e digest anterior. Reversão que reintroduza versão vulnerável mantém o acesso público em manutenção.

## T05 — Logs úteis sem credenciais

**Interfaces:** entrada HTTP não confiável; saída de diagnóstico com campos limitados. Não criar coletor/supervisor.

**Modificar:** `deploy/jellyfin-proxy/nginx.conf`, template Caddy da borda, testes do relay. Criar `tests/integration/test_jellyfin_proxy_security.py` usando a reprodução com sentinela da auditoria como referência.

- [ ] Reproduzir falha atual em Nginx descartável com token fictício e upstream fechado; esperar 502 e sentinela no erro antes da correção.
- [ ] Usar access log JSON escapado com método, status, duração, upstream status e classe de rota. Não registrar URI arbitrária: tokens também podem ser colocados no path, headers e mensagens malformadas.
- [ ] Desativar persistência de erros HTTP Nginx que contenham requisição, inclusive no contexto que trata requests inválidas. Nginx OSS não ganha sanitização por mudar o nível para `crit`. Manter diagnóstico por access log sanitizado e verificação de configuração no deploy.

```nginx
# Base da política; validar todos os contextos com a fixture real.
error_log /dev/null;
http {
    log_format safe escape=json
      '{"method":"$request_method","status":$status,'
      '"duration":"$request_time","upstream_status":"$upstream_status"}';
    access_log /dev/stdout safe;
}
```

- [ ] No Caddy, registrar apenas campos aprovados, removendo request URI e headers completos; manter exclusão dos logs de erro HTTP brutos. Testar stdout, stderr, arquivos e journal aplicável. Usar rotação nativa, 10 MB × 3 arquivos e acesso restrito. [Configuração de logs Caddy](https://caddyserver.com/docs/caddyfile/directives/log).
- [ ] Criar teste parametrizado para 502, timeout, request inválida, path/query codificados, token em headers e handshake WebSocket. Procurar o marcador literal e codificado em todos os sinks; conferir que o JSON continua parseável e o status de erro permanece observável.

```python
@pytest.mark.parametrize("case", ["closed_upstream", "timeout", "bad_request", "websocket"])
def test_no_request_secret_reaches_any_log(nginx_security_fixture, case):
    result = nginx_security_fixture.exercise(case, sentinel="AUDIT_ONLY_51f3")
    assert result.completed
    assert "AUDIT_ONLY_51f3" not in result.all_logs
    assert result.sanitized_status_count >= 1
```

A fixture é criada nesse mesmo arquivo, com container temporário, nenhum segredo real e sem publicação externa. Nenhum teste lê logs de produção para procurar tokens reais.

**Aceite:** regressão real não encontra sentinela em nenhum sink. **Rollback:** reverter roteamento separadamente; não reativar logging bruto para diagnosticar incidente.

## T06 — Separar relay e reduzir distribuição de segredos

**Interfaces:** novo `environment_for_service(role: str, configured: dict[str, str]) -> dict[str, str]`; novo `create_relay_app(base_url: str) -> FastAPI`. O relay usa somente URL interna e parâmetros de apresentação, sem `ControlState`, banco ou credencial administrativa.

**Arquivos:** `service_environment.py`, `runtime.py`, `render.py`, novo `api/jellyfin_relay.py`, `api/app.py`, `api/jellyfin_labels.py`, Nginx; testes `test_configuration_runtime.py`, `test_stack_render.py`, novo `test_service_environment.py`, novo `test_jellyfin_relay_isolation.py`.

- [ ] Levantar consumidores reais por papel antes de cortar variáveis; `Field.consumer` hoje é documentação, não controle de acesso. Separar runtime contínuo do `init`/operador de configuração.
- [ ] Escrever testes de allowlist e falha fechada para papel desconhecido/credencial nova:

```python
def test_relay_does_not_receive_global_secrets():
    configured = {
        "HOMESERVER_ADMIN_PASSWORD": "sentinel-admin",
        "HOMESERVER_NEW_SECRET": "sentinel-new",
        "HOMESERVER_JELLYFIN_URL": "http://jellyfin:8096",
    }
    actual = environment_for_service("jellyfin-relay", configured)
    assert "sentinel-admin" not in actual.values()
    assert "sentinel-new" not in actual.values()
```

- [ ] Gerar ambiente privado estruturado por papel a partir do `.env`; não criar `*_FILE`. Remover `/project` dos processos contínuos e parar recarga automática de todos os fields editáveis. Preservar valores literais com `$`, `#`, aspas e Unicode; render privado fica fora do Git e com modo 0600.
- [ ] Extraír registro das rotas de labels/WS para `jellyfin-relay`, reaproveitando código/testes existentes. Nginx deixa de enviar navegação comum ao `control-api`. Relay sem mídia, banco, diretório de runtime privilegiado e credenciais no ambiente.
- [ ] Manter exclusão coordenada no controlador privado e recusar DELETE de mídia na entrada pública. Nenhum `/downloads`, `/config` ou endpoint de controlador é registrado no relay.
- [ ] Validar trust de proxy em rede dedicada. Resolver IPs atuais com falha fechada; onde Nginx exigir identidade estável, usar IPAM explícito em subnet escolhida após detectar conflitos. Não confiar no subnet inteiro nem manter IP Docker incidental.
- [ ] Em containers de fixture, comprovar ausência de `.env` e credenciais por booleans/nomes de campos, sem imprimir valores. Telemetria/métricas não devem receber segredos; API/gateway/worker recebem somente dependências comprovadas.

**Aceite:** navegar/assistir funciona com controlador indisponível no ensaio, exceto funções privadas que dependem dele; relay não acessa segredos nem administração. **Rollback:** restaurar conjunto coerente de Compose/código; público permanece fechado se voltar a atravessar processo privilegiado sem os controles planejados.

## T07 — Integrações mínimas e autorização de exclusão

**Interfaces:** Seerr consome `http://automation-proxy:8082/radarr` e `http://automation-proxy:8082/sonarr` usando `baseUrl` da integração. Proxy repassa credencial do próprio Seerr e nunca injeta chave mais privilegiada. `create_automation_proxy_app(upstreams: dict[str, str]) -> FastAPI` aceita somente os dois upstreams configurados; `validate_arr_request(service: str, method: str, path: str, body: bytes) -> None` rejeita fora do contrato com erro sem corpo/credenciais. Controlador conserva seus endpoints diretos privados.

**Arquivos:** novos `api/automation_proxy.py` e `api/integration_policy.py` dentro de `homeserver_control`; `render.py`; `configuration/media_servers.py`; `api/deletion_capture.py`; criar `tests/contract/test_automation_proxy.py` e `tests/integration/test_seerr_request_boundary.py`.

- [ ] Executar Seerr de fixture contra *arr de fixture e registrar **somente método, rota normalizada e nomes de campos**, cobrindo conectar, sincronizar, listar perfis/pastas, consultar disponibilidade, pedir filme/série/temporada e reconciliar um pedido existente.
- [ ] Escrever allowlist fechada dessas operações em `integration_policy.py`. GET não significa seguro: negar configurações/chaves/logs e filesystem browser; permitir apenas rotas comprovadas. Para POST/PUT, validar schema/campos do corpo e limites de tamanho, sem regex JSON. Preservar `preventSearch=true`, pastas/perfis aprovados e deixar busca/download sob o controlador. Negar `/command` genérico. Novas rotas da versão upstream falham fechadas até revisão do contrato.
- [ ] Apontar Seerr ao proxy e remover rotas diretas a Radarr/Sonarr tanto por DNS Docker quanto por portas do host. Recriar o teste de pedido completo; se um fluxo exigir acesso destrutivo amplo, não relaxar para `location /` — registrar o conflito e manter esse fluxo fora do público até resolver o contrato.
- [ ] Recusar destinos absolutos externos, métodos inesperados, caminhos codificados/normalizados ambíguos e redirects para outro upstream. Não criar proxy aberto com URL fornecida pelo cliente.
- [ ] Testar DELETE público de mídia com token ausente, viewer, admin antigo e admin novo: sempre negado antes de chegar ao controlador. Mesmos casos na entrada privada continuam sujeitos à autorização e à coordenação existente; usar mídia fictícia para exclusão.

```python
@pytest.mark.parametrize("method,path", [
    ("DELETE", "/api/v3/movie/123"),
    ("POST", "/api/v3/command"),
    ("GET", "/api/v3/config/host"),
])
def test_seerr_cannot_use_administrative_routes(automation_proxy_fixture, method, path):
    result = automation_proxy_fixture.request(method, path)
    assert result.status_code in (403, 404, 405)
    assert not automation_proxy_fixture.upstream_received_request
```

O proxy é um processo Compose mínimo da imagem já usada pelo projeto, separado do relay/controlador, sem banco, mídia ou credencial administrativa em ambiente. Não é daemon do host. Respostas mantêm status e payload nativos necessários ao Seerr, sem seguir redirects; erros não incluem corpo, headers ou tokens. Testar `baseUrl` nas duas integrações e garantir que o reconciliador não volte a gravar o destino direto.

**Aceite:** pedidos funcionam em fixture; nenhum caminho direto/destrutivo permanece. **Risco residual:** adicionar pedidos continua sendo uma capacidade autorizada; quotas/política de aprovação permanecem explícitas, sem alterações silenciosas.

## T08 — Redes e firewall do HomeServer

**Interfaces:** consome matriz da especificação e contratos T06/T07; produz redes com membros mínimos, política persistente e resultados de conexões permitidas/negadas.

**Arquivos:** `render.py`, Compose efetivo, `test_stack_render.py`, novo `tests/integration/test_public_network_isolation.py`, runbook de segurança; regras reais somente em configuração nativa do host e registro privado.

- [ ] Substituir redes amplas compartilhadas por redes de relação: proxy↔relay/Jellyfin, Seerr↔Jellyfin, Seerr↔proxy de integração e proxy↔*arr. Não compartilhar uma `egress` entre todos os serviços públicos/administrativos. Conferir também telemetry e caminhos por host ports.
- [ ] Aplicar política para INPUT do host e FORWARD Docker no backend existente; verificar posição das regras diante dos ACCEPT do Tailscale/Docker. Não mudar backend nem desativar a gestão de regras do Docker.
- [ ] Negar origens públicas/containers públicos a SSH, controlador, qBit, Prowlarr/Bazarr e demais hosts, incluindo endereços IPv6 globais locais. Permitir respostas estabelecidas e dependências documentadas. Manter downloads/peers do qBit em seu escopo atual.
- [ ] Garantir que redes entre pares não autorizem novas conexões no sentido inverso por acidente. Aplicar e testar restrições também entre containers da mesma bridge; identificar se o tráfego passa pelos hooks escolhidos, não presumir que uma regra FORWARD o vê.
- [ ] Eliminar acesso direto externo ao backend Seerr: a porta publicada passa ao `seerr-proxy`, com origens e headers controlados. Manter portas privadas necessárias somente em interfaces/origens autorizadas.
- [ ] Testar como UID Caddy e dentro de Seerr/relay/Jellyfin: positivos da matriz conectam; negativos por nome, IPv4, IPv6 e endereço do host falham. Comparar contadores do firewall para comprovar qual regra bloqueou.
- [ ] Recriar containers/redes de fixture e repetir. Depois testar persistência usando mecanismo nativo da distribuição e reboot controlado na janela aprovada, com recuperação disponível. Comprovar também ordem de boot: as regras de bloqueio precisam existir antes de listeners/portas Docker aceitarem tráfego. Regras dependentes de redes são preparadas sem janela de liberação; se o backend exigir mecanismo que ainda não existe, resolver esse requisito com o mecanismo da distribuição e documentar antes de publicar, sem inventar unit do projeto.

**Aceite:** não basta `internal: true`, inspeção estática ou timeout externo isolado. Exigir provas de conexão e regra efetiva para cada fronteira. **Rollback:** remover/restaurar apenas cadeias próprias, sem flush global; falha de egress/isolamento não autoriza liberar tudo para destravar o app.

## T09 — HTTPS privado, proteção de sessão e contas

**Interfaces:** novo `reconcile_jellyfin_security(settings, client, mode)` e `reconcile_seerr_security(settings, client, mode)` em `configuration/security.py`, chamados por `reconcile_native_settings`. Seguem os objetos de resultado e modo já usados no projeto. `public_access.py` define `PublicAccessGuard.check(request: Request) -> Response` para Jellyfin e `SeerrPublicAccessGuard.check(request: Request) -> Response` para Seerr: 204 somente para usuário comum validado, 401 sem sessão, 403 para administrador/chave de serviço, 503 para falha de validação; resposta nunca inclui identidade/token. O processo de guard pode consultar apenas os endpoints fixos de identidade dos dois apps, sem chave própria.

**Arquivos:** `configuration/security.py`, `configuration/reconcile.py`, `media_servers.py`, `settings.py`, exemplos/documentação; Seerr proxy; `api/public_access.py`, `api/jellyfin_relay.py`, Nginx; novos `tests/contract/test_security_reconcile.py`, `tests/integration/test_seerr_security.py`, `tests/contract/test_public_access_guard.py`; ampliar contratos de exclusão/paths.

- [ ] Antes do CSRF, fornecer HTTPS privado ao Seerr. Caminho recomendado: Tailscale Serve no HomeServer para um listener loopback dedicado do `seerr-proxy`; verificar porta disponível, HTTPS da tailnet e ausência de Funnel. Preservar configurações Serve existentes, sem reset global.
- [ ] O listener privado aceita apenas Serve/loopback e saneia headers; não confiar automaticamente em qualquer cliente local/Docker. Validar login/cookie com a origem Tailscale real. Na LAN, validar o domínio HTTPS atual na 54443 por hairpin. Alternativa: DNS local do mesmo nome apontando à TV box **com URL privada na porta 443**, certificado válido e regras de origem privada explícitas; DNS sozinho não transforma 54443 em 443. Configuração no roteador é aplicada pelo proprietário. Não ligar CSRF enquanto um acesso privado necessário depender de HTTP autenticado.
- [ ] Distinguir acesso público por hairpin de administração privada: hairpin continua sujeito ao guard público. Bypass privado exige listener/política cuja origem LAN/Tailscale seja comprovada; jamais aceitar header enviado pelo cliente ou considerar automaticamente o IP do roteador como administrador. Se o roteador mascarar clientes externos como origem privada, a separação precisa de listener/interface privada distinta antes da aprovação. Testar LAN sem Tailscale e Tailscale de fora de casa.
- [ ] Só então ativar `csrfProtection` e `trustProxy` do Seerr, com backend direto inacessível. HTTP antigo passa a orientar/redirecionar para a URL privada HTTPS sem encaminhar senha/cookie por query.
- [ ] Definir cinco tentativas Jellyfin como proposta gerenciada, aplicar por ID às contas no escopo e preservar atributos não gerenciados. Testar bloqueio/desbloqueio em contas descartáveis; não tentar senhas inválidas em contas reais.
- [ ] Configurar conta administrativa separada, remota desabilitada e redes locais/tailnet verificadas. Antes de revogar sessões, inventariar **tipo e consumidores**, sem valores: `discover_native_credentials()` pode ter salvo AccessToken administrativo como `HOMESERVER_JELLYFIN_API_KEY`. Preparar credencial substituta pelo mecanismo nativo, atualizar `.env` e ambientes por papel, recriar apenas consumidores necessários e validar worker/coleções/importação/recuperação USB/Seerr. Só depois revogar a credencial anterior; não revogar token de finalidade desconhecida às cegas. Manter publicação em manutenção durante a transição.
- [ ] Reproduzir em fixture a diferença entre autorização padrão e `RequiresElevation`: testar `/System/Logs` e `/System/Info/Storage` com sessão administrativa emitida na rede privada. A leitura do código indica que a flag remota não basta; tratar como teste de regressão obrigatório, não depender dessa flag para todas as rotas.
- [ ] Implementar `PublicAccessGuard` em serviço Compose mínimo `public-access`, definido em `api/public_access.py` e renderizado por `render.py`, sem segredos, banco, mídia ou montagem do projeto. Para cada requisição pública autenticada, consultar **upstream fixo** `/Users/Me` com credencial do próprio cliente e origem validada. Aceitar apenas resposta 200 com ID de usuário válido e `Policy.IsAdministrator` explicitamente falso; ausência de campo, API key sem usuário, erro, redirect ou timeout negam. Não adicionar autenticação própria, guardar senhas, emitir sessões ou trocar por token administrativo. Inicialmente sem cache de decisões; medir custo no playback antes de qualquer otimização.
- [ ] Nginx usa uma subrequest interna de autorização, sem corpo, antes das rotas públicas protegidas e do handshake WS. A subrequest não é uma URL pública; encaminha autenticação nativa com a mesma precedência de header/query validada em fixture. Negar credenciais duplicadas/ambíguas. A consulta não recebe URL de upstream do cliente e não encaminha o corpo original.
- [ ] Mapear explicitamente falhas do guard na configuração Nginx: `auth_request` trata status fora de 2xx/401/403 como erro; provar que a resposta final é erro fechado (500/503 conforme mapeamento) e nunca encaminha ao app. O 503 do contrato do guard é medido diretamente e não presumido como repasse automático do Nginx.
- [ ] Enumerar as exceções anônimas a partir do fluxo da versão alvo (assets `/web/`, informação pública, login e descoberta estritamente necessários), com métodos fixos; nenhum wildcard em `/Users`, `/System` ou `/Items`. Login nativo deve continuar recusando administrador de origem pública. Entradas privadas usam listener/cadeia de proxy distintos, comprovados pela origem de rede, sem um header controlado pelo cliente que desative o guard.
- [ ] Testar a composição guard + Nginx real + Jellyfin alvo. Token administrativo em header ou `ApiKey`, chave de serviço, campos ausentes e falha de `/Users/Me` devem bloquear antes do endpoint final. Viewer autenticado deve reproduzir e usar WS; a indisponibilidade do guard deve falhar fechado e gerar diagnóstico sem segredo. Se qualquer rota contornar essa composição, não retomar publicação.
- [ ] Aplicar `SeerrPublicAccessGuard` na entrada pública Seerr: consultar `/api/v1/auth/me` com cookie nativo do cliente, recusar API key pública e identidade com permissão administrativa/gestão de usuários/configuração conforme as constantes da versão 3.5.0. Somente exceções anônimas de assets/status/login comprovadas passam sem sessão; acesso autenticado passa pela verificação inclusive fora de `/api`. Não repassar API key de integração para resolver identidade. Testar cookies antigos/novos, todas as formas de autenticação habilitadas e preservação do login/pedido do usuário comum. Para login administrativo público que a API nativa aceite, não liberar uso subsequente da sessão; testar que o endpoint de login não retorna dados/segredos administrativos e bloquear esse mecanismo se retornar.
- [ ] A verificação de identidade não pode recursar no próprio proxy: os dois guards consultam endpoints internos fixos sem passar pela entrada pública. Atualizar a matriz/redes de T08 para permitir somente esses destinos ao serviço `public-access`, com suas entradas acessíveis apenas aos proxies. O relay de mídia permanece em processo separado, alcançando somente Jellyfin. Repetir os testes de egress após introduzir esse serviço.
- [ ] Manter usuários ocultos, sem senha fácil e sem privilégios novos. Conferir importação/cadastro de usuários Seerr e remover apenas mecanismos de autenticação comprovadamente não usados; preservar login do proprietário e integrações. Não instalar SSO/Fail2ban como substituto desses controles.
- [ ] `plan` não escreve; `apply` faz merge dos campos gerenciados e readback; `verify` detecta drift. API sem capacidade exigida deve produzir falha explícita, sem inventar campo ou editar banco diretamente.

```python
async def test_security_plan_is_read_only(native_security_fixture):
    result = await native_security_fixture.reconcile(mode="plan")
    assert result.has_changes
    assert native_security_fixture.mutating_requests == []

async def test_reapply_preserves_accounts_and_unmanaged_policy(native_security_fixture):
    before = native_security_fixture.account_identity_snapshot()
    await native_security_fixture.reconcile(mode="apply")
    await native_security_fixture.reconcile(mode="apply")
    assert native_security_fixture.account_identity_snapshot() == before
    assert native_security_fixture.second_apply_mutations == []
```

```python
@pytest.mark.parametrize("identity", ["admin_session", "server_api_key", "missing_policy"])
async def test_public_guard_never_admits_privileged_or_unknown_identity(public_guard_fixture, identity):
    response = await public_guard_fixture.check(identity=identity)
    assert response.status_code == 403
    assert not public_guard_fixture.protected_endpoint_called

async def test_guard_outage_fails_closed(public_guard_fixture):
    response = await public_guard_fixture.check(identity="viewer", upstream_timeout=True)
    assert response.status_code == 503
    assert not public_guard_fixture.protected_endpoint_called
```

**Aceite:** sessão Seerr tem Secure/HttpOnly/SameSite apropriado; POST legítimo funciona e cross-origin indevido é recusado; usuário comum não vê configurações/chaves. Admin funciona pela entrada privada e falha pela pública inclusive com token antigo. Limite de login e recuperação têm evidência em fixture.

## T10 — Recursos, filesystem e manutenção

**Interfaces:** consome imagens e matriz já validadas; produz limites por serviço e exceções justificadas.

**Arquivos:** `render.py`, Compose/overlays, settings para preferências editáveis, testes de storage/subtitle/finalização, runbooks. Usar hardening por serviço, sem aplicar indiscriminadamente a init/operador.

- [ ] Medir memória/processos/CPU e I/O durante navegação, scan e transcode representativo; registrar pico/p95 e reserva disponível do host. Para cada serviço, escolher limite com margem mínima de 50% sobre o pico observado e soma das reservas dentro da RAM disponível para aplicativos. Se não couber, reduzir a concorrência ensaiada/negociar capacidade, sem inventar limite seguro.
- [ ] Aplicar `no-new-privileges`, `cap_drop: [ALL]` e rootfs somente leitura onde a imagem suportar, com tmpfs/cache/config/transcode necessários. Limites de PID também consideram processos de FFmpeg; não limitar GPU/CPU de modo a invalidar reprodução sem medir.
- [ ] Validar acesso Intel `/dev/dri` e grupos existentes, sem `privileged`, RTX ou dispositivos extras. Manter binds de mídia conforme autorização atual; documentar que o Jellyfin ainda pode escrever neles.
- [ ] Corrigir permissões de configurações com segredos e diretórios ancestrais, preservando os UIDs necessários; não usar `chmod/chown -R` sobre mídia inteira. Testar leitura negada a usuário sem vínculo em fixture.
- [ ] Revisar headers de segurança somente após compatibilidade: `nosniff`, política de referência, anti-frame e CSP em report-only sanitizado quando viável. Não ativar HSTS/preload automaticamente em host cuja única porta pública HTTPS seja não padrão; comprovar efeito em redirecionamentos, clientes e recuperação antes de persistir política no navegador.
- [ ] Documentar revisão manual periódica de avisos/imagens e procedimento de renovação/teste Dynu, sem scheduler/CI novo. Atualizações passam pelos mesmos checks de compatibilidade e rollback.

**Aceite:** reprodução direta/transcode/legenda/pedido não sofrem OOM nem loop de restart; memória e processos têm limites efetivos conferidos em HostConfig/cgroups. **Rollback:** reverter o limite do serviço afetado com evidência, preservando as outras restrições.

## T11 — Validação integrada e retomada

**Interfaces:** consome todas as entregas A; produz relatório privado com cada critério PASS/FAIL/NOT TESTED e registros sanitizados. Retomada pública só depois dos critérios críticos passarem.

**Arquivos:** suítes novas e existentes; novo `tests/system/test_public_security.py`; `.runtime/security-hardening-20261015/acceptance.json`; runbooks e `SECURITY.md` atualizados.

- [ ] Executar em Linux/WSL2, com binários reais usados pelos contratos e fixtures sem dados reais:

```bash
uv sync --frozen
uv run make lint
uv run make test-unit
uv run make test-contract
uv run make test-integration
uv run make compose-check
uv run make smoke
bash scripts/check-secrets.sh
```

`make compose-check` verifica o base; acrescentar `docker compose ... config --quiet` com os quatro arquivos e ambiente de fixture. Skips por ausência de Docker/Caddy não contam como aprovado. Scanners opcionais usam imagens explícitas e o mecanismo existente `scripts/check-images.sh`; findings aplicáveis são revisados, não descartados por um exit code favorável.

- [ ] Repetir a matriz de spoofing da auditoria: XFF/X-Real-IP/Forwarded, headers duplicados/listas, SNI/Host divergente, URL absoluta, métodos, paths codificados, TLS antigo e requests malformadas. O controle da rede pode bloquear antes do 403; testar separadamente cada camada em fixture.
- [ ] Testar de uma origem externa permitida e outra não permitida. Para IPv6, validar regra efetiva e conexão a endereços globais conhecidos. Porta não respondendo por um único vantage não comprova fechamento permanente.
- [ ] Validar autenticação viewer/admin, sessão antiga, lockout recuperável, cookies/CSRF, execução negativa de operações administrativas e todos os atalhos por portas do host.
- [ ] Validar navegação/capas, reprodução direta, seek/Range, WS, transcode, áudio/legenda, sessão na TV e pedidos Seerr. Testes que criariam downloads/exclusões ficam em fixture; uso real não altera fila/mídia.
- [ ] Repetir negativos após recriação de containers e reinício controlado de hosts. Confirmar DDNS/renovação de certificado, acesso Tailscale, recuperação de mounts e progresso normal dos downloads preexistentes.
- [ ] Comparar catálogo/contas/pedidos e volumes com T01. Publicar somente a configuração aprovada, observar uma sessão de reprodução e uma janela de operação sem erros novos; registrar duração real, não alegar estabilidade de dias sem observação.
- [ ] Se um controle crítico falhar, manter/restaurar manutenção pública e operar por LAN/Tailscale. Distinguir correção concluída, risco residual e teste pendente. Nada neste plano garante ausência absoluta de vulnerabilidades futuras.

## T12 — Segmentação externa e escrita de mídia

Esta é uma entrega de desenho executivo e viabilidade; não introduz mudanças de router/mount sem autorização específica.

- [ ] Conferir no equipamento real suporte a VLAN/ACL entre segmentos, isolamento Wi-Fi e IPv6. Preparar matriz edge↔servidor↔LAN: edge só alcança os dois upstreams, servidor não inicia conexões aos dispositivos pessoais, administração entra da rede/identidade autorizada.
- [ ] Incluir todas as interfaces da TV box: Ethernet e Wi-Fi precisam pertencer ao segmento correto ou a interface redundante deve ficar desativada na futura mudança aprovada. Revisar grants/ACL Tailscale para a identidade da borda permitir apenas upstreams; negar SSH/painéis/demais nós e não anunciar a LAN inteira. Repetir negativos pelos caminhos físico IPv4/IPv6 e overlay, pois root pode usar o Tailscale da própria máquina para contornar uma VLAN.
- [ ] Se suportado, documentar portas físicas, VLANs, subnets sem conflito, DHCP/DNS, recuperação e passos manuais do proprietário. Separar também HomeServer da LAN pessoal se a meta incluir comprometimento root do servidor.
- [ ] Se não suportado, comparar equipamento de segmentação com VPS externa/túnel restrito. Para VPS, medir banda/latência/custo antes de escolher; túnel permite somente serviços previstos, sem anunciar a LAN inteira. Domínio passa a apontar à VPS e o proprietário remove encaminhamentos residenciais somente depois do aceite.
- [ ] Ensaiar a matriz de bloqueio a partir do segmento de borda usando máquina descartável; não obter root explorando o servidor real. Confirmar que a barreira está fora do host que se deseja conter.
- [ ] Desenhar evolução de mídia RO com escritor privado para legendas/edições, paths canônicos, prevenção de traversal/symlink e exclusão ainda coordenada. Inventariar consumidores antes de propor alteração dos binds. Não tratar rootfs RO ou chmod de arquivo como proteção contra exclusão em diretório gravável.

**Aceite separado:** T12 produz projeto executivo/viabilidade, não implanta rede ou mounts. A Entrega B só fica aprovada após implementação posterior autorizada e barreira externa testada. Até lá, o relatório A registra “aplicativos endurecidos; isolamento de host não concluído”. Mudança para mídia RO exige revisão própria e autorização de escopo.

## Checklist de revisão do plano

- [x] S01–S10 possuem tarefas e critérios de aceite.
- [x] Migração não reativa auth legada e rollback não reutiliza DB migrado.
- [x] CSRF não promete operações autenticadas por HTTP privado.
- [x] Proxy de integração mantém requests legítimos e trata API keys amplas.
- [x] Logs de erro e tokens em path também entram nos testes.
- [x] Docker, IPv6, reboot e IPs efêmeros têm verificações próprias.
- [x] Escrita de mídia e comprometimento root têm riscos residuais explícitos.
- [x] Plano não altera router, discos, fila, segredos ou serviços durante sua elaboração.

## Referências de correção

Os avisos completos e sua aplicabilidade estão no relatório privado. Fontes principais para validar os pisos de correção durante T04: [Jellyfin SVG, corrigido em 12.0](https://github.com/jellyfin/jellyfin/security/advisories/GHSA-cf3c-8m59-2vhx), [Jellyfin streaming, corrigido em 10.11.7](https://github.com/jellyfin/jellyfin/security/advisories/GHSA-jh22-fw8w-2v9x), [Seerr avatar proxy, corrigido em 3.4.0](https://github.com/seerr-team/seerr/security/advisories/GHSA-mc6w-69r3-62h8). A gravidade depende das pré-condições descritas; não rotular esses três casos como RCE anônimo indistinto.

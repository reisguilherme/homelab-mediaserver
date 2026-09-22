# HomeServer — Implementação 01: ambiente, infraestrutura e implantação inicial

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Executar os checkboxes em ordem e registrar evidências por tarefa.

**Goal:** preparar o desenvolvimento no desktop e adotar a base existente do Legion sem reinstalar o sistema ou alterar seu layout de discos.

**Architecture:** ferramentas Linux no WSL2 produzem configuração Compose, scripts Bash e imagens. O servidor recebe versões identificadas por SSH/Tailscale; uma unidade systemd só inicia a stack após validar a montagem esperada.

**Tech Stack:** WSL2/Ubuntu, Git, Python 3.12/uv/pytest, Bash/ShellCheck, Docker Compose e systemd.

**Spec:** [Escopo v1.3](../specs/2026-09-21-homeserver-design.md); ler também as [restrições e contratos do plano principal](2026-09-21-homeserver-implementation.md).

## Restrições globais

- Somente `/srv/data` para mídia; sem mergerfs, formatação ou migração de disco.
- Preservar Tailscale SSH e OpenSSH LAN existentes.
- Preservar conservação Lenovo, configurações logind e blacklist da GPU.
- A RTX 2060 não será utilizada.
- Scripts de auditoria não aplicam mudanças; instalação só ocorre no modo explícito de aplicação.

## I01 — Inventário e adoção do servidor

**Arquivos a criar:** `scripts/audit-server.sh`, `scripts/check-mount.sh`, `config/server.example.yaml`, `docs/runbooks/server-baseline.md`, `tests/system/test_mount_guard.sh`.

**Consome:** resumo fornecido pelo usuário e acesso SSH já funcional. **Produz:** inventário local `config/server.local.yaml` ignorado pelo Git, relatório de auditoria e guarda da montagem.

Contrato dos comandos:

```text
bash scripts/audit-server.sh --check
  0: todos os requisitos obrigatórios presentes
  2: inventário obtido, há diferenças ou requisitos ausentes
  1: erro de coleta que impede decisão
bash scripts/check-mount.sh PATH UUID
  0: PATH é exatamente um mountpoint, UUID corresponde e está gravável
  1: qualquer condição não satisfeita
```

- [ ] No desktop, preservar as mudanças existentes de documentação no Git e estabelecer o checkout de trabalho no WSL2.
- [ ] Criar `.gitignore` antes de coletar inventário: excluir `*.local.yaml`, `*.local.env`, `.env`, `secrets/`, `.runtime/`, `.artifacts/private/`, `.venv/` e arquivos de estado dos serviços.
- [ ] Implementar auditoria de leitura das informações abaixo, redigindo um relatório legível e uma saída JSON quando `--json` for solicitado. Não imprimir chaves, cookies, tokens ou conteúdo de `authorized_keys`.

Comandos de coleta a incorporar com tratamento individual de falha:

```bash
cat /etc/os-release
uname -r
id
docker version
docker compose version
findmnt --json --target /srv/data --output TARGET,SOURCE,UUID,FSTYPE,OPTIONS
df -B1 /srv/data /srv/appdata /srv/transcode /srv/backup-staging
lsblk --json --output NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS
systemctl is-active ssh tailscaled docker lenovo-conservation.service
systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target
systemd-analyze cat-config systemd/logind.conf
stat -c '%u:%g %a %n' /srv/data /srv/appdata /srv/transcode /srv/backup-staging
ls -l /dev/dri
lspci -nnk
```

Não confundir `findmnt --target` apontando para `/` com `/srv/data` montado. Usar `findmnt --mountpoint` e comparar o UUID explicitamente na guarda. Modos 775 sem UID/GID compatíveis não asseguram escrita aos containers.

- [ ] Implementar `check-mount.sh` com argumentos obrigatórios, `findmnt`, comparação exata e falha se a montagem estiver somente leitura. UUID incorreto ou vazio bloqueia a stack; não tentar montar ou corrigir o disco nesse script.
- [ ] Criar testes em VM descartável: diretório comum no root é recusado; filesystem de teste com UUID incorreto é recusado; montagem correta é aceita; somente leitura é recusada. Não desmontar `/srv/data` real para testar.
- [ ] Coletar via SSH somente quando o usuário fornecer o destino; usar alias `homeserver` configurado no cliente. Exemplo de transporte: `ssh homeserver 'bash -s -- --check' < scripts/audit-server.sh`. Este é um alias a configurar, não um host presumido existente.
- [ ] Registrar Ubuntu/kernel, UUID/fstype, UID/GID, grupos `render`/`video`, interfaces LAN/Tailscale, capacidade e as lacunas. Classificar cada verificação como `reported`, `verified` ou `missing`.
- [ ] Validar pelo menos uma sessão OpenSSH LAN e uma Tailscale SSH. Para esta última, verificar regras de rede **e** regras SSH da tailnet; não inferir autorização a partir de uma chave pública do OpenSSH. [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh)

**Saída/aceite:** relatório sem segredos e preservação da configuração atual. A01, A17 e A34.

## I02 — Ambiente local, bootstrap e CI inicial

**Arquivos:** `.gitattributes`, `.gitignore`, `AGENTS.md`, `README.md`, `pyproject.toml`, `uv.lock`, `Makefile`, `scripts/bootstrap-server.sh`, `scripts/lib/bootstrap.sh`, `tests/system/test_bootstrap.sh`, `tests/fixtures/server-valid.yaml`, `.github/workflows/ci.yml`.

**Consome:** inventário I01. **Produz:** comandos locais consistentes, script de adoção e reconstrução e testes executados em CI sem contato com produção.

- [ ] Configurar finais de linha LF para `*.sh`, `*.yaml`, `*.yml`, `Dockerfile*` e `Makefile`; executar Bash no WSL2.
- [ ] Inicializar o workspace Python 3.12 com `uv`; dependências próprias: FastAPI, Uvicorn, Pydantic, HTTPX, PyYAML; desenvolvimento: pytest, pytest-asyncio e Ruff. Resolver e versionar `uv.lock` na implementação, depois utilizar `--frozen`.
- [ ] Definir comandos `make lint`, `make test-unit`, `make test-contract`, `make test-integration`, `make compose-check` e `make smoke`. O primeiro conjunto chama Ruff, pytest, ShellCheck, `bash -n` e Compose sem segredos de produção.
- [ ] Implementar CLI do bootstrap:

```text
bootstrap-server.sh --check --config FILE
bootstrap-server.sh --plan --config FILE
bootstrap-server.sh --apply --mode adopt --config FILE
bootstrap-server.sh --apply --mode fresh --config FILE
```

`--check` e `--plan` não alteram o host. `adopt` é o modo do Legion atual; `fresh` serve à reconstrução futura em máquina limpa. Ambos exigem montagem previamente preparada para declarar a base concluída.

- [ ] Funções públicas de `scripts/lib/bootstrap.sh`: `detect_platform`, `check_packages`, `check_services`, `check_power`, `check_storage`, `plan_changes`, `apply_missing`, `write_report`. Cada função retorna sucesso/falha e o relatório registra antes/depois.
- [ ] Em `adopt`, instalar apenas dependências faltantes. Não fazer `apt upgrade`, regenerar initramfs, escrever fstab, trocar a versão Docker ou refazer login Tailscale implicitamente.
- [ ] Em `fresh`, instalar dependências por repositórios oficiais com chaves verificadas e permitir preparar OpenSSH/Docker/Tailscale. A autorização Tailscale continua explícita; o script não recebe uma chave embutida.
- [ ] Gerenciar somente arquivos próprios. Diante de configuração preexistente conflitante, sair com motivo e proposta de alteração; não substituir `server-power.conf` ou `lenovo-conservation.service` automaticamente.
- [ ] Tratar privilégio, lock de pacote, ausência de internet e interrupção. Usar lock exclusivo do bootstrap, backups de arquivos alterados e logs sem secrets. Não reiniciar o host automaticamente.

Caso mínimo executável de CLI, a adaptar ao diretório real de teste:

```bash
set -euo pipefail
before=$(sha256sum "$fixture_root/etc/fstab")
bash scripts/bootstrap-server.sh --check --config tests/fixtures/server-valid.yaml
after=$(sha256sum "$fixture_root/etc/fstab")
test "$before" = "$after"
```

`fixture_root` é criado pelo harness de teste e passado às funções de acesso a arquivos; o modo de teste não pode estar habilitado em produção por uma variável ambiente não documentada. Testar efeitos reais de pacotes/systemd em VM descartável, além dos testes de funções.

- [ ] Testar primeira instalação em VM, segunda execução sem diferenças e recuperação após falha simulada de rede. Com `adopt`, conferir os hashes dos arquivos existentes antes/depois.
- [ ] CI inicial em executor hospedado executa lint, unidade e contratos locais. Não distribuir credenciais de produção para testes de pull request.

**Saída/aceite:** A31–A34; arquivos não gerenciados preservados, nenhum sucesso declarado com dependência obrigatória pendente.

## I03 — Compose local e contratos de filesystem

**Arquivos:** `deploy/compose.yaml`, `deploy/compose.dev.yaml`, `deploy/compose.prod.yaml`, `config/versions.env`, `config/dev.env.example`, `scripts/verify-layout.sh`, `tests/integration/test_layout.py`, `docs/runbooks/service-setup.md`.

**Consome:** UID/GID e inventário, mapa de serviços do plano principal. **Produz:** stack local isolada e configuração de produção renderizada, ainda sem downloads automáticos.

- [ ] Selecionar imagens mantidas para Jellyfin, Seerr, Arr, Bazarr, qBittorrent e Mosquitto; registrar versão e digest em `config/versions.env`. Não adotar `latest` em produção. Registrar usuário suportado por imagem e sua versão de API.
- [ ] Criar Compose base sem portas publicadas, sem caminhos do host e sem dispositivos GPU. Dev e prod adicionam valores específicos. Conferir o resultado combinado, pois listas e mounts possuem regras próprias de merge. [Compose merge](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/)
- [ ] Dev usa somente diretórios em `.runtime/dev` no WSL2 e portas em `127.0.0.1`; usa arquivos de teste, credenciais locais e nunca URLs de produção.
- [ ] Prod monta `/srv/data` uma única vez como `/data` nos componentes que importam/downloadam, preservando caminhos. Usar bind mounts longos com `create_host_path: false` para não criar um diretório quando a origem estiver ausente.

Contrato exemplificado para um serviço que precisa de `/data`:

```yaml
volumes:
  - type: bind
    source: /srv/data
    target: /data
    bind:
      create_host_path: false
```

`create_host_path: false` não comprova que o disco está montado; a guarda I01 continua obrigatória.

- [ ] Provisionar grupo de mídia compatível, diretórios com setgid quando necessário e umask compatível. Ajustar apenas diretórios necessários, sem `chown -R` genérico de `/srv` ou `chmod 777`.
- [ ] Montar segredos somente nos serviços consumidores; verificar suporte `_FILE` por imagem. Onde não existir, usar configuração local protegida e impedir que o render de Compose com segredos entre nos logs. [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [ ] Configurar uma instância de Sonarr e Radarr, perfil único 2160p/1080p e upgrades desativados. No Seerr, não habilitar servidor 4K separado e desabilitar busca automática durante esta etapa. Uma única biblioteca pode conter 4K e 1080p. [Seerr — serviços](https://docs.seerr.dev/using-seerr/settings/services/)
- [ ] Validar links usando usuários reais dos containers: criar arquivo de teste pequeno em `/data/torrents`, importar por hardlink em `/data/media`, comparar device/inode e apagar cada referência separadamente. Executar em diretório exclusivo de teste, com limpeza restrita a ele.

Teste de semântica Linux a incluir em `test_layout.py`:

```python
import os

def test_hardlink_preserves_payload_after_unlink(tmp_path):
    original = tmp_path / "download.mkv"
    imported = tmp_path / "library.mkv"
    original.write_bytes(b"fixture")
    os.link(original, imported)
    assert original.stat().st_ino == imported.stat().st_ino
    assert original.stat().st_dev == imported.stat().st_dev
    original.unlink()
    assert imported.read_bytes() == b"fixture"
```

Esse teste unitário de filesystem é complementado pelo teste dentro dos containers; sozinho não comprova as permissões em produção.

**Comandos futuros:** `make compose-check`, `make test-integration`, `bash scripts/verify-layout.sh --environment dev`. **Saída:** I03 concluída quando o perfil dev não contém mounts/URLs de produção e os hardlinks passam. A18.

## I04 — Primeiro deploy e proteção de inicialização

**Arquivos:** `scripts/deploy.sh`, `scripts/smoke.sh`, `deploy/systemd/homeserver-stack.service`, `deploy/systemd/homeserver-mount-watch.service`, `deploy/systemd/homeserver-mount-watch.timer`, `docs/runbooks/manual-deploy.md`.

**Consome:** artefato identificado e inventário I01. **Produz:** stack de teste implantada manualmente, healthchecks e inicialização protegida.

- [ ] Criar release em `/opt/homeserver/releases/<commit>` e apontador `/opt/homeserver/current`; configurações locais ficam em `/etc/homeserver` e dados em `/srv`. O marcador `<commit>` na documentação representa o SHA real recebido e validado pelo script, não um nome literal.
- [ ] `deploy.sh --release SHA --artifact FILE --config FILE` valida SHA, checksum, configuração, segredo necessário, montagem, capacidade e imagens antes de trocar a release. Não usar `git reset --hard` no checkout de trabalho do usuário.
- [ ] Escolher supervisor único: containers persistentes com `restart: "no"`; unidade systemd executa `docker compose up --abort-on-container-exit` em primeiro plano, com `Restart=always`, atraso de 10 segundos e `ExecStartPre` chamando `check-mount.sh`. Parar a unidade para manutenção deve parar os containers via `ExecStop=... compose stop`.
- [ ] Adicionar `RequiresMountsFor=/srv/data`, `After=docker.service network-online.target tailscaled.service`. Validar prontidão de Docker/Tailscale e bind IP antes de iniciar. Resolver a unidade de mount pelo caminho e configurar vínculo/monitoramento para parar a stack se o filesystem esperado desaparecer.
- [ ] Um timer de guarda a cada 15 segundos verifica UUID e leitura do volume; se inválido, interrompe a stack. O controlador também valida a montagem antes de cada admissão. Não usar apenas existência do diretório como healthcheck.
- [ ] Registrar o trade-off: nessa versão, a saída de um serviço persistente reinicia a stack inteira após a guarda. É simples e evita containers reiniciarem sozinhos antes da validação do disco; observar impacto no teste contínuo. Jobs finitos rodam fora dessa unidade.
- [ ] Publicar Jellyfin na LAN e Tailscale conforme endereços do inventário; Seerr e interface administrativa conforme política. Painéis Arr devem ficar em loopback/túnel ou Tailscale restrito. qBittorrent não publica sua API diretamente na LAN.
- [ ] Validar acesso LAN, Tailscale e origem não autorizada, inclusive IPv6. Nenhum teste requer redirecionar porta no roteador.
- [ ] Simular mount ausente em VM: `systemctl start homeserver-stack` falha antes de criar containers; reinício de Docker não revive containers por fora da unidade. Simular crash de processo, reinício do host e ausência temporária do IP Tailscale.

**Aceite:** A01, A17, A25, A27. Ainda não liberar aquisições reais; manter fixtures e downloads de teste controlados.

## I05 — Intel UHD e matriz de reprodução

**Arquivos:** `docs/runbooks/jellyfin-hardware.md`, `docs/evidence/playback-matrix.md`, `scripts/verify-gpu.sh`; modificar `deploy/compose.prod.yaml` e versões quando necessário.

**Consome:** render device Intel identificado e Jellyfin implantado. **Produz:** aceleração demonstrada e limites iniciais de reprodução.

- [ ] Mapear o render node Intel pelo dispositivo PCI/driver, sem assumir `renderD128`; validar acesso pelo usuário do Jellyfin e grupos numéricos do host.
- [ ] Configurar QSV e usar VA-API se a combinação validada exigir. Confirmar uso real do hardware por logs do FFmpeg e ferramenta de diagnóstico da GPU; CPU baixa sozinha não é prova.
- [ ] Testar H.264 1080p, HEVC 4K SDR, HEVC 10-bit HDR com conversão para SDR e legenda que exige burn-in. AV1 não será perfil preferido nessa geração. [Jellyfin Intel](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/intel/)
- [ ] Medir duração de 30 minutos por combinação simultânea relevante: 4K/4K direto, 4K direto + 1080p convertido, duas conversões 1080p. Registrar origem, codec, bitrate, cliente, FPS, temperatura, buffer e direct/relay.
- [ ] Validar iPhone 16 Pro, Galaxy S25, Book3 e TV Samsung identificada. Para viagens, testar saída HDMI do Book3 e capacidade de negociação do cabo/adaptador.
- [ ] Registrar perfil remoto por cliente e orçamento de banda; 4K/4K é objetivo condicionado a bitrate e rede, não requisito de duas transcodificações 4K.

**Aceite:** A03 e base de A04–A06. Resultados inadequados exigem ajustar perfis e retestar somente as combinações afetadas.

## Conclusão deste subplano

Commitar cada tarefa com os arquivos específicos e suas evidências sanitizadas. A base pode ser considerada adotada após I04; a capacidade de reprodução só será declarada após I05. Seguir para C01 com a stack ainda restrita a testes.

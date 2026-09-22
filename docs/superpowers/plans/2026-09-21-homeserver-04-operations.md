# HomeServer — Implementação 04: backup, implantação, CI/CD e aceite

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Executar os checkboxes com evidências e preservar dados persistentes.

**Goal:** tornar o projeto recuperável, permitir implantações versionadas e comprovar o funcionamento no servidor real.

**Architecture:** snapshots consistentes são enviados por Restic ao desktop Windows 11. A implantação manual validada vira uma operação de GitHub Actions com executor temporário no Tailscale. Testes e build são automáticos; produção é atualizada por acionamento explícito.

**Tech Stack:** Bash, systemd, Restic/SFTP, GitHub Actions, Tailscale SSH, Docker Compose e scripts de smoke test.

**Spec:** [Escopo v1.3](../specs/2026-09-21-homeserver-design.md), seções 16–18; [plano principal](2026-09-21-homeserver-implementation.md).

## Restrições globais

- Backup de configurações e bancos, sem mídia e sem cache de transcodificação.
- Desktop pode estar desligado; isso não deve impedir operação do servidor.
- Nenhum push ou pull request dispara deploy automático na primeira versão.
- Tailscale SSH já está ativado no Legion; suas regras de autorização são diferentes de `authorized_keys` do OpenSSH LAN.
- Atualização preserva `/srv/data` e `/srv/appdata`. Retorno de imagem não desfaz migração de banco.

## O01 — Backup consistente e restauração

**Arquivos:** `scripts/backup.sh`, `scripts/restore.sh`, `deploy/systemd/homeserver-backup.service`, `deploy/systemd/homeserver-backup.timer`, `docs/runbooks/backup-restore.md`, `tests/integration/test_backup_restore.py`, `docs/evidence/restore-report.md`.

**Consome:** diretórios persistentes, serviços implantados e desktop Windows. **Produz:** cópia externa verificável e restauração sem aquisição indevida.

- [ ] Configurar no Windows 11 um destino SFTP com usuário dedicado e pasta de backup; testar permissões e espaço. Usar credencial própria e restringir o acesso ao servidor. Não depender de sessão interativa aberta para disponibilizar o serviço quando o desktop estiver ligado.
- [ ] Instalar Restic se ausente e criar repositório criptografado. Guardar senha e chave SFTP fora do Git e cópia da chave de recuperação fora do Legion. Conferir identidade do servidor SFTP.
- [ ] Definir manifesto versionado de dados: configurações/bancos de todas as aplicações, estado qBittorrent, banco do controlador, outbox de alertas, configuração/segredos necessários, firmware/configuração e versões implantadas.
- [ ] Excluir mídia, cache, logs extensos, arquivos temporários e tokens de sessão dispensáveis. Registrar tamanho esperado e estimativa de espaço no desktop antes do primeiro envio.
- [ ] Gerar cópia consistente: usar exportação nativa comprovada ou uma janela curta de parada. Para a primeira implementação, escolher cópia com stack parada; copiar appdata para staging e reiniciar antes de comprimir/enviar ao desktop. Parar/iniciar pela unidade systemd de I04, evitando que o supervisor reinicie containers durante a cópia.
- [ ] Executar preferencialmente fora de reprodução ativa. Se houver sessões, adiar com limite e notificar envelhecimento do backup; não interromper silenciosamente a reprodução para cumprir um horário.
- [ ] Instalar trap de recuperação: se a stack estava ativa antes do backup, tentar restaurar seu estado ao sair, inclusive em falha de cópia. Nunca iniciar uma stack que já estava parada por intervenção do usuário sem registrar a decisão.
- [ ] Antes de parar, confirmar capacidade de staging. Gerar checksum/manifesto e publicar a geração pronta por rename; diretório incompleto nunca é elegível para envio.
- [ ] Tentar backup diário e envio pendente de hora em hora, com lock exclusivo. Retenção externa: 7 diárias, 4 semanais e 3 mensais; staging até 3 gerações/20 GB. Se a geração exceder o orçamento, avisar e preservar último backup válido, sem encher o disco.
- [ ] Desktop desligado deve gerar estado pendente sem derrubar serviços. Sucesso de cópia local não atualiza o horário de backup externo bem-sucedido.

Interface dos scripts:

```text
backup.sh --config FILE --capture-and-send
backup.sh --config FILE --send-pending
backup.sh --config FILE --verify-repository
restore.sh --config FILE --snapshot ID --target PATH --isolated
```

Não executar restore diretamente por cima da instalação ativa. `--target` precisa ser um diretório de recuperação explicitamente permitido; recusar `/`, `/srv/data`, symlinks e o appdata em uso. Validar manifestação de versão e requisitos de migração antes de iniciar os serviços restaurados.

- [ ] Criar ensaio de restauração em projeto Compose isolado, com banco e caminhos temporários, sem portas públicas, sem rede de peers e sem worker de aquisição ativo. Restaurar uma cópia não autoriza novos downloads.
- [ ] Conferir contas, pedidos, políticas, tombstones, reservas e lista de torrents. Reconciliar com arquivos reais antes de qualquer retomada. Reservas restauradas não autorizam consumo com base em medidas antigas de espaço.
- [ ] Registrar perdas esperadas se o snapshot preceder uma exclusão recente; produzir relatório de diferenças e exigir revisão antes de liberar o scheduler. Não recriar automaticamente mídia ausente depois de restaurar um backup antigo.

Teste essencial:

```python
def test_restored_controller_starts_with_admission_disabled(restored_instance):
    status = restored_instance.get("/health/ready").json()
    assert status["admission_enabled"] is False
    assert restored_instance.pending_side_effects() == []
```

`restored_instance` é uma fixture que inicia a imagem em modo `recovery`, com banco restaurado e adaptadores de efeitos externos desabilitados. A interface `/health/ready` deve incluir `admission_enabled` em todos os modos e retorna 503 enquanto a admissão estiver bloqueada por recuperação. `/health/live` e a interface de diagnóstico permanecem disponíveis, com motivo explícito.

**Aceite:** A23, A24. Evidências: snapshot externo legível, integridade verificada e teste de recuperação concluído. Executar antes de introduzir biblioteca real.

## O02 — Releases, smoke tests e retorno de versão

**Arquivos:** `scripts/deploy.sh`, `scripts/rollback.sh`, `scripts/smoke.sh`, `config/release.schema.json`, `docs/runbooks/releases.md`, `tests/integration/test_deploy.py`.

**Consome:** implantação manual I04, imagens e backup O01. **Produz:** procedimento único reutilizado tanto pelo desktop quanto pelo CI.

Contrato do manifesto de release:

```json
{
  "schema_version": 1,
  "git_commit": "1111111111111111111111111111111111111111",
  "database_schema": 3,
  "requires_backup": true,
  "images": {},
  "config_checksums": {},
  "artifact_sha256": "2222222222222222222222222222222222222222222222222222222222222222"
}
```

Valores de SHA acima são dados de teste. Um manifesto de produção exige mapa de imagens completo com digest e checksums reais; mapas vazios são rejeitados. `artifact_sha256` referencia o payload da release excluindo o próprio manifesto, evitando checksum autorreferente.

- [ ] Validar origem, checksum, commit testado, imagens, schema do banco e compatibilidade de configuração antes de parar a stack. Verificar segredo por presença sem imprimir valor.
- [ ] Impedir deploy concorrente com `flock`; conservar release atual, manifesto e último snapshot antes de migrar.
- [ ] Preparar release nova sem editar arquivos da release anterior. Colocar appdata, secrets e mídia fora das pastas de release.
- [ ] Verificar diffs locais não registrados e parar em caso de divergência; não sobrescrever correção manual silenciosamente.
- [ ] Suspender admissões, drenar operações críticas do controlador, capturar backup se requerido, parar a stack e aplicar migrações versionadas. Operação de exclusão em curso precisa chegar a checkpoint persistente antes da troca.
- [ ] Iniciar nova versão e executar smoke: montagem/UUID, APIs vivas, banco/schema, autenticação, reserva de teste sem efeito remoto e leitura de uma mídia de teste.
- [ ] Readiness de controlador deve retornar 503 se invariantes obrigatórias falharem; smoke não exige indexador externo sempre disponível para atestar processos, mas registra serviços externos degradados.
- [ ] Falha pós-deploy mantém registro de erro e executa o caminho de retorno validado: se schema compatível, voltar imagem/configuração; caso contrário, restaurar snapshot consistente com a release anterior antes de iniciar.
- [ ] Preservar tarefas/tombstones criados durante uma tentativa parcialmente ativa. Evitar abrir admissões ao usuário antes de concluir smoke e commit da release.

Contrato de CLI:

```text
deploy.sh --release SHA --artifact FILE --config FILE
rollback.sh --release SHA --config FILE --snapshot ID
smoke.sh --config FILE --environment dev|prod
```

`rollback` exige snapshot quando a compatibilidade do banco não estiver demonstrada. Não copiar biblioteca de mídia nem executar `docker compose down -v` em produção.

- [ ] Ensaiar imagem com healthcheck falhando e migração deliberadamente incompatível em dev; demonstrar recuperação e ausência de perda de tombstone/reserva.
- [ ] Registrar tempo de indisponibilidade e passos que exigiram intervenção, sem prometer rollback automático universal.

**Aceite:** A27 e A29; atualização não altera arquivos de mídia e a versão em execução é identificável.

## O03 — GitHub Actions com acesso privado

**Arquivos:** `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`, `docs/runbooks/github-actions.md`, `tests/system/test_workflow_policy.py`.

**Consome:** O02 validada manualmente e repositório privado. **Produz:** CI automático e CD acionado explicitamente para um commit validado.

- [ ] CI em push/pull request executa lint, unidade, contratos, integração em containers e render do Compose dev. Testes de hardware permanecem fora do executor hospedado.
- [ ] Build das imagens próprias usa lockfile e commit identificável; publicar artefatos/imagens de releases autorizadas com digest. Pull requests não recebem credenciais para produção.
- [ ] Workflow de implantação somente por `workflow_dispatch`, com input de SHA/artefato. Validar que corresponde à branch/release confiável e a um resultado de CI aprovado; falha ou CI ausente bloqueia o job.
- [ ] Fixar actions por SHA completo revisado e limitar permissões do `GITHUB_TOKEN`. Não executar scripts de um fork não confiável com segredos de implantação.
- [ ] Usar executor hospedado conectado ao Tailscale com identidade efêmera. Preferir federação de identidade quando disponível na conta; alternativa é OAuth armazenado em secrets. A integração permite acesso sujeito à tag/regra da tailnet. [Tailscale GitHub Action](https://tailscale.com/docs/integrations/github/github-action)
- [ ] Configurar tag exclusiva de CI, destino exclusivo no Legion e usuário Linux dedicado à implantação. Com Tailscale SSH ativo, criar regra SSH não interativa específica para esse fluxo, além da permissão de rede. Regra `check` que exige confirmação humana interativa pode impedir o job; não trocar regras da conta pessoal por uma liberação ampla.
- [ ] Não presumir que `authorized_keys` controla sessões Tailscale SSH. Se for adotado OpenSSH em porta separada para CI, documentar a mudança e manter o Tailscale SSH existente; essa alternativa só será usada se o teste demonstrar necessidade.
- [ ] O usuário dedicado não recebe automaticamente grupo `docker` ou sudo irrestrito. Encaminhar implantação a uma entrada administrada, com parâmetros/paths validados. Registrar que a autoridade de alterar Compose e serviços privilegiados pode equivaler a administração do host; uma conta com outro nome não elimina esse poder.
- [ ] Validar transferência de artefato por SFTP/SCP sobre a sessão escolhida, identidade do host e comando de implantação. Não abrir SSH no roteador nem instalar runner persistente no Legion.
- [ ] Configurar `concurrency` para produção sem cancelar um deploy em andamento. O lock no servidor continua obrigatório, inclusive para bloquear concorrência com deploy manual.
- [ ] Executar o mesmo `deploy.sh` validado em O02; não manter uma implementação paralela escondida no YAML.
- [ ] Falha de acesso/rede marca execução como falha; não avançar marcador de release. Logs mostram SHA, etapa e resultado sem segredos. Limpar artefatos temporários conforme política, sem apagar a release anterior necessária à recuperação.

Estrutura do workflow a implementar:

```yaml
name: Deploy HomeServer
on:
  workflow_dispatch:
    inputs:
      commit:
        description: Commit validado pelo CI
        required: true
        type: string
concurrency:
  group: homeserver-production
  cancel-in-progress: false
permissions:
  contents: read
  actions: read
  id-token: write
```

Os jobs concretos são implementados nesta tarefa com os SHAs reais das actions e contratos O02. O fragmento define a política de disparo e concorrência; não é um workflow pronto.

**Aceite:** A28 e A30; provar que PR não implanta, SHA sem CI não implanta, identidade CI não acessa outros nós e um deploy não interrompe outro.

## O04 — Aceite final e manual de operação

**Arquivos:** `docs/evidence/acceptance.md`, `docs/runbooks/operations.md`, `docs/runbooks/recovery.md`, `docs/evidence/soak-test.md`; atualizar README e matriz de reprodução.

**Consome:** todas as tarefas anteriores. **Produz:** evidências A01–A34 e operação documentada.

- [ ] Executar fluxo completo de filme, temporada concluída e temporada em lançamento com fontes de teste verificáveis, mantendo tetos e reservas.
- [ ] Simular capacidade insuficiente em ambiente isolado; provar pedido pendente, exclusão manual e retomada. Não preencher o SSD real indiscriminadamente para simular disco cheio.
- [ ] Excluir uma temporada de série ainda monitorada e repetir sincronização, reinício e restauração de teste; nenhuma reposição automática deve ocorrer.
- [ ] Testar perda de API, gateway, worker, MQTT e Wi-Fi separadamente. Verificar estados desconhecidos, alertas e manutenção da reprodução/seeding quando aplicável.
- [ ] Validar duas reproduções reais com download e seeding ativos, via LAN e rede móvel. Registrar limite de bitrate por cliente, conexão direta/relay, temperatura, buffer e qualquer redução para 1080p.
- [ ] Executar teste contínuo de 48–72 horas com restart planejado, backup e CYD ativos. Conferir crescimento de logs/cache, memória, saúde do SSD e orçamento de espaço.
- [ ] Documentar limites conhecidos: conteúdo sem metadados verificáveis, dependência das fontes/legendas, limite de bitrate remoto, dispositivos ainda não validados e ausência de alerta externo para falha total do host.
- [ ] Criar manual para solicitar, ver motivo de espera, excluir pelo fluxo coordenado, verificar alertas, implantar, voltar versão, restaurar e atualizar a CYD.
- [ ] Preencher cada critério A01–A34 com data, ambiente, versão, evidência e resultado. Um item bloqueado permanece explicitamente aberto; não marcar o projeto concluído se requisito obrigatório estiver sem validação.

Modelo de registro:

```text
Critério: A19
Versão: SHA e digests implantados
Ambiente: integração isolada / Legion
Preparação: temporada de teste com pacote e hardlinks conhecidos
Ação: exclusão coordenada e reconciliação após reinício
Evidência: IDs removidos, bytes liberados, tombstone persistente
Resultado: aprovado/reprovado, com causa concreta
```

Concluir com commits dos relatórios sanitizados e versões utilizadas. O projeto estará entregue quando a suíte, as regras adicionais, a CYD e a recuperação tiverem evidências suficientes; disponibilidade dos containers sozinha não representa esse resultado.

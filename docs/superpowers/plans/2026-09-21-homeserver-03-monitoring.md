# HomeServer — Implementação 03: monitoramento, alertas e CYD

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Executar os checkboxes por tarefa com as evidências indicadas.

**Goal:** mostrar saúde, transferências, capacidade e reproduções no painel físico e enviar alertas úteis aos celulares.

**Architecture:** coletor consulta APIs e um snapshot de métricas produzido no host, gera JSON compacto e publica por MQTT. A ESP32 assina tópicos de leitura; a API administrativa e o controlador de mídia permanecem separados.

**Tech Stack:** Python 3.12, HTTPX, Pydantic, cliente MQTT, Mosquitto com TLS, ESPHome/LVGL, pytest e testes físicos na ESP32-2432S028.

**Spec:** [Escopo v1.3](../specs/2026-09-21-homeserver-design.md), seções 13–14; [plano principal](2026-09-21-homeserver-implementation.md), contrato de telemetria.

## Restrições globais

- CYD somente para monitoramento, sem comando de pause, exclusão, restart ou administração.
- Percentual desconhecido não vira zero; temperatura não disponível não vira leitura normal.
- Sem socket Docker ou credenciais administrativas na CYD.
- Falha do coletor/painel não interrompe reprodução e seeding.
- Limite de seeding inicial de 20 Mbps; 5 Mbps durante reprodução remota, sujeitos ao teste real.

## M01 — Coletor e contrato de telemetria

**Arquivos:** `services/telemetry/src/homeserver_telemetry/{app,collectors,models,publisher,bandwidth}.py`, `scripts/host-metrics.py`, `deploy/systemd/homeserver-metrics.service`, `deploy/systemd/homeserver-metrics.timer`, `docs/contracts/telemetry-v1.schema.json`, `tests/unit/test_telemetry.py`, `tests/integration/test_bandwidth.py`.

**Consome:** APIs e estado do controlador; snapshot somente leitura do host. **Produz:** `GET /api/v1/telemetry` no serviço telemetry, tópico MQTT e ajuste do limite de upload através do controlador.

Exemplo de contrato versionado, com valores fictícios de teste:

```json
{
  "schema_version": 1,
  "sequence": 42,
  "generated_at": "2026-09-21T15:00:00Z",
  "sources": {
    "qbittorrent": {"status": "ok", "age_seconds": 1},
    "host": {"status": "ok", "age_seconds": 4},
    "jellyfin": {"status": "unknown", "age_seconds": 35}
  },
  "host": {"cpu_percent": 12.4, "ram_percent": 31.0, "cpu_celsius": 52.0, "uptime_seconds": 7200},
  "capacity": {"total_bytes": 512000000000, "free_bytes": 300000000000, "reserved_unallocated_bytes": 100000000000, "admissible_bytes": 174400000000},
  "transfers": {"download_bps": 12000000, "upload_bps": 625000, "active_count": 1, "queue_count": 3},
  "downloads": [{"id": "job-1", "title": "Conteúdo de teste", "progress": 0.42, "download_bps": 12000000, "eta_seconds": null, "state": "downloading"}],
  "playback": {"active_count": null, "remote_count": null, "items": []},
  "alerts": [{"id": "jellyfin-stale", "severity": "warning", "message": "Estado do Jellyfin desatualizado"}]
}
```

Valores do exemplo de capacidade usam margem de 5% de 512 GB = 25,6 GB. O coletor recebe o cálculo do controlador; não reimplementa o ledger de reservas com outra fórmula.

- [ ] Escrever JSON Schema com tipos, unidades, limites e nulabilidade; mensagens inválidas não são publicadas como válidas.
- [ ] Implementar consulta de transfers a cada 5 segundos, host/serviços/sessões a cada 15 segundos, com timeouts e isolamento de falhas por fonte. Não bloquear todas as fontes esperando uma API indisponível.
- [ ] Produzir `/run/homeserver/host.json` por processo restrito no host, com escrita em arquivo temporário e rename atômico. Incluir CPU/RAM/discos/temperaturas e horário; executar diagnósticos SMART menos frequentemente, por exemplo a cada hora.
- [ ] Timer/coletor do host lê fontes locais conhecidas, sem aceitar comandos vindos de HTTP. Se precisar de privilégio para NVMe, limitar a esse serviço, sem expor acesso root à aplicação web.
- [ ] Montar somente o diretório do snapshot como leitura no container telemetry. Medidas do container não podem ser apresentadas como CPU/RAM globais do notebook.
- [ ] Integrar capacidade e estados da fila pelo contrato do controlador. Caso o controlador esteja indisponível, exibir `unknown` e a idade, não espaço reservado igual a zero.
- [ ] Obter sessões Jellyfin e distinguir reprodução direta/transcodificação; classificar remoto usando endereços/rede conhecidos. Proxy deve preservar origem conforme configuração; classificação ambígua usa limite conservador.
- [ ] Solicitar alteração de upload por comando interno autenticado do controlador: `set_seed_limit(bytes_per_second: int, reason: str)`. O controlador aplica pela API qBittorrent/gateway autorizada e verifica o resultado.
- [ ] Com sessão remota ou estado de sessões desatualizado, aplicar 625.000 B/s; sem sessão remota confirmada, até 2.500.000 B/s. Exigir 60 segundos sem sessão remota antes de aumentar, evitando oscilação.
- [ ] Configurar qBittorrent com limite persistente conservador. Se o coletor morrer após elevar o limite, o controlador detecta heartbeat vencido e volta ao limite de 625.000 B/s. Se ambos falharem, permanece o último limite persistido, nunca upload ilimitado.

Teste de staleness:

```python
from homeserver_telemetry.models import source_status

def test_old_success_is_reported_as_stale():
    assert source_status(last_success_age=31, last_error=None) == "stale"

def test_missing_source_is_unknown():
    assert source_status(last_success_age=None, last_error=None) == "unknown"
```

Contrato `source_status(last_success_age: float | None, last_error: str | None) -> str`: retorna `unknown`, `ok`, `stale` ou `error`, sem apagar a idade da última leitura válida.

**Aceite:** A20–A22; teste de unidade com relógio simulado e teste real confirmando limite efetivo no cliente durante reprodução remota.

## M02 — Alertas e credenciais de telemetria

**Arquivos:** `services/telemetry/src/homeserver_telemetry/{alerts,notifications}.py`, `deploy/mosquitto/mosquitto.conf`, `deploy/mosquitto/acl`, `tests/unit/test_alerts.py`, `tests/integration/test_mqtt_acl.py`, `docs/runbooks/notifications.md`.

**Consome:** eventos próprios de disponibilidade/erro, saúde dos serviços, backup e storage. **Produz:** alertas deduplicados e broker limitado à finalidade do painel.

- [ ] Implementar outbox persistente no serviço de notificações com chave `(event_type, object_id, generation)`, estados pending/sent/failed e retry com backoff. Não gravar token ou URL com token no evento.
- [ ] Importar eventos pelas rotas internas do controlador e persistir cursor junto com os eventos locais antes do ACK. Testar queda entre importação, confirmação e envio. Um timeout após aceitação pelo provedor pode duplicar uma entrega externa; documentar esse caso em vez de prometer entrega exatamente uma vez.
- [ ] Alertar conteúdo pronto somente a partir de `media.available` de C05. Para série, agrupar rajada de episódios/temporada em janela inicial de 60 segundos.
- [ ] Alertar serviço após três falhas consecutivas; disco abaixo da margem imediatamente; backup externo mais antigo que 72 horas; pedidos bloqueados por causa nova. Evento de recuperação encerra o incidente anterior.
- [ ] Telegram é o adaptador inicial recomendado; confirmar uso da conta antes de configurá-lo. Desenvolver e testar com transporte falso. Alternativa de canal não deve alterar o formato interno de eventos.
- [ ] Testar entrega com aplicativo em segundo plano no iPhone e Android. Não usar um teste de resposta HTTP da API como prova de notificação visível no telefone.
- [ ] Broker usa TLS e CA local, autenticação por dispositivo/serviço e sem acesso anônimo. Certificado contém nome/IP configurado do broker; planejar renovação e gravação da CA na CYD.

ACL mínima alvo:

```text
user telemetry-publisher
topic write homeserver/v1/server/#

user cyd-01
topic read homeserver/v1/server/#
topic write homeserver/v1/cyd-01/availability
```

O publisher envia `homeserver/v1/server/snapshot` e `homeserver/v1/server/availability`. Configurar Last Will de disponibilidade, QoS adequado, retain do último snapshot e limite de mensagem. Credenciais distintas por cliente, fora do Git. A CYD publicar presença não lhe dá permissão de comandar serviços.

- [ ] Implementar testes MQTT: CYD lê snapshot; não publica em `server/#`; não assina tópicos de outros usuários; autenticação inválida é negada; reconexão recebe snapshot retido mas o timestamp continua sendo validado.
- [ ] Restringir listener TLS à LAN necessária à CYD e rede Docker; não abrir MQTT publicamente. Testar memória e handshake na placa antes de fixar buffer/tamanho final.

**Aceite:** A22 e parte de A25. Monitor interno não notifica falha total do host/internet; registrar essa limitação no manual.

## M03 — Firmware e telas da ESP32-2432S028

**Arquivos:** `firmware/cyd/cyd.yaml`, `firmware/cyd/packages/{display,touch,mqtt,ui}.yaml`, `firmware/cyd/secrets.example.yaml`, `firmware/cyd/fixtures/`, `docs/runbooks/cyd-flash.md`, `docs/evidence/cyd-acceptance.md`.

**Consome:** contrato telemetria v1, broker e placa identificada. **Produz:** firmware compilado, gravado e validado no hardware.

- [ ] Identificar revisão exata, controlador da tela/touch, tamanho de flash e presença real de PSRAM; fotografar/registrar referência local quando disponível. Não aplicar pinout de outra variante nem habilitar PSRAM por suposição.
- [ ] Criar primeiro firmware mínimo: tela, toque, Wi-Fi e relógio. Fixar versão do ESPHome e toolchain no ambiente de build; compilar no desktop.
- [ ] Gravar por USB conectado ao desktop; definir método de acesso serial no Windows/WSL2 ou usar ferramenta de gravação suportada. Depois validar atualização autenticada pela rede, com procedimento de recuperação USB.
- [ ] Integrar MQTT autenticado/TLS e JSON versionado. Assinar somente os dois tópicos do servidor. ESPHome possui componente MQTT; o teste de integração deve usar a versão fixada. [Documentação](https://esphome.io/components/mqtt/)
- [ ] Limitar snapshot para a placa a 8 KiB, até quatro itens de download (ativos primeiro, máximo dois ativos), títulos truncados e alertas prioritários. Exibir total da fila e acesso aos detalhes completos na interface web; não carregar toda a biblioteca na RAM da ESP32.
- [ ] Criar telas: resumo/alerta prioritário, downloads, capacidade física/reservada, recursos do servidor e sessões Jellyfin. Navegação por toque e redução de brilho; não incluir botões de escrita.
- [ ] Usar `eta_seconds: null` como “—”; progresso desconhecido como indeterminado; velocidades e tamanhos com unidades. Diferenciar download parado de fila vazia e serviço indisponível.
- [ ] Guardar última sequência/timestamp. Snapshot retido antigo não pode fazer o painel parecer atualizado: usar horário sincronizado e comparar idade; sem relógio válido, indicar aguardando sincronização. Mais de 30 segundos sem leitura atualizada gera aviso visível.
- [ ] Testar desconexão Wi-Fi, queda do broker, reinício do servidor, JSON inválido, versão de schema desconhecida, títulos longos, caracteres acentuados e todos os campos indisponíveis.
- [ ] Medir memória e estabilidade com TLS/LVGL e atualizações por pelo menos 24 horas; reduzir buffers/imagens se necessário. Não depender de pôsteres ou animações para o aceite.

Comandos futuros, depois da criação dos arquivos e configuração dos segredos locais:

```bash
esphome config firmware/cyd/cyd.yaml
esphome compile firmware/cyd/cyd.yaml
```

**Aceite:** A21. Evidência física: percentual/velocidade conferidos com qBittorrent, alerta de dados antigos, reconexão e navegação funcionando. A compilação sozinha não conclui M03.

## Conclusão deste subplano

Executar testes de modelo/alertas, integração das ACLs e aceite físico da placa. Registrar fonte e idade das métricas e a ausência de controle remoto pela CYD. Prosseguir à validação operacional e backups sem transformar a disponibilidade do painel em dependência da reprodução.

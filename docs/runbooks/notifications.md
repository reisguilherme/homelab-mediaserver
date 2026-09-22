# Alertas e telemetria

Alertas próprios são deduplicados por `(event_type, object_id, generation)` e passam por outbox persistente. O payload interno não guarda tokens de provedores nem URLs com credenciais. Um timeout depois de um provedor aceitar a mensagem pode gerar duplicação externa; o operador deve tratar a chave de geração como a referência idempotente.

Telegram é o adaptador inicial recomendado, mas a conta ainda precisa ser confirmada. O transporte falso é a única dependência dos testes locais. A disponibilidade da notificação não é pré-requisito para reprodução ou seeding.

No perfil de produção, o broker MQTT usa o template TLS
`deploy/mosquitto/mosquitto.prod.conf`, certificados/`passwd` montados de
`/etc/homeserver/mqtt-certs`, autenticação por dispositivo/serviço, ACL de
leitura limitada e mensagens de até 8 KiB para a CYD. O perfil dev usa listener
interno sem publicação; TLS de produção só pode ser declarado após validar o
certificado e o handshake na rede real. O painel só lê snapshots e publica
presença; não há tópicos de comando administrativo.

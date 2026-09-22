# CYD: identificação, build e recuperação

Antes de compilar, registrar revisão exata da ESP32-2432S028, controlador de display/touch, flash, PSRAM e pinout. `firmware/cyd/cyd.yaml` é deliberadamente um esqueleto até essa identificação; não presumir `board`, PSRAM ou pinos de outra variante.

```bash
esphome config firmware/cyd/cyd.yaml
esphome compile firmware/cyd/cyd.yaml
```

O primeiro flash deve ser por USB conectado ao desktop, com uma cópia da configuração de secrets fora do Git. Depois de validar TLS/MQTT, OTA deve ser autenticado e o procedimento de retorno USB deve permanecer documentado. Aceite físico ainda exige testes de snapshot antigo, JSON inválido, Wi-Fi/broker indisponível, títulos acentuados e 24 horas de estabilidade.

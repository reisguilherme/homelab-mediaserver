# Operação diária

- Consultar a fila e o motivo de espera antes de repetir um pedido.
- Tratar `waiting_space`, `waiting_source`, `waiting_subtitles` e estados
  `unknown` como estados operacionais, não como falhas a ignorar.
- Usar sempre o preview de exclusão e confirmar a mesma versão; a exclusão é
  manual e coordenada com monitoramento, Arr, seeding e tombstones.
- Conferir capacidade física, compromissos e idade do snapshot de telemetria.
- Não liberar downloads enquanto UUID, banco, gateway ou backup estiverem
  bloqueados.

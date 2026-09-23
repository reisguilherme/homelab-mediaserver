# Ensaio de restauração

O ensaio automatizado local verifica captura, manifesto, checksum e restauração
isolada com `admission_enabled=false`. A verificação foi executada com fixtures;
SFTP/Restic, migrações e reconciliação da biblioteca no Legion ainda exigem a
infraestrutura real.

Em 2026-09-22, um snapshot real do Legion foi copiado para o desktop via SSH.
`restic check` terminou sem erros no repositório local e o arquivo `COMMIT` de
um release foi restaurado em diretório isolado e conferido (41 bytes). Um
segundo snapshot foi capturado pelo serviço systemd, copiado pela chave SSH
restrita e verificado novamente no Windows. Os testes de migração e
reconciliação da biblioteca após restauração completa ainda estão pendentes.

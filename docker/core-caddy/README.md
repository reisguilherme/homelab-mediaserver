# Caddy privado sem file capabilities

Esta receita preserva os bytes do Caddy da imagem original autenticada e cria
um novo inode sem os atributos estendidos do binário de origem. O qbit-monitor
escuta internamente na porta 8080; não precisa de `CAP_NET_BIND_SERVICE`.
O Compose ativo continua com UID 1000, `cap_drop: [ALL]` e
`no-new-privileges:true`. Esta receita não altera Compose, configuração, rotas,
portas ou autenticação nativa do qBittorrent.

`COPY` pode preservar metadados; a etapa intermediária escreve somente os bytes
em arquivo novo, compara com `cmp` e aplica modo 0755. A etapa final remove o
inode herdado antes de copiar esse artefato. Não instala dependências, não
recompila Caddy e não usa rede. A referência Docker descreve a
[cópia de arquivos e metadados](https://docs.docker.com/reference/dockerfile/#copy);
o Linux armazena capacidades de arquivo em
[`security.capability`](https://man7.org/linux/man-pages/man7/capabilities.7.html).

O operador deve usar contexto mínimo contendo apenas esta receita, resolver um
tag local para o ID completo autenticado da imagem original e conferir esse
ID imediatamente antes e depois do build. O argumento `BASE_IMAGE` não tem
default e não deve apontar a um tag remoto mutável. Usar `--pull=false` e
`--network=none`; nenhuma instrução inicia serviços. A sintaxe é compatível
com o builder legado usado no guest, sem `COPY --chmod` ou frontend externo.

```sh
docker build --pull=false --network=none \
  --build-arg BASE_IMAGE=LOCAL_AUTHENTICATED_ORIGINAL_TAG \
  -f Dockerfile -t LOCAL_PRIVATE_RESULT_TAG MINIMAL_CONTEXT
```

Antes de selecionar a imagem final para execução, registrar em evidência
privada o ID original, ID final, SHA256 desta receita e hashes do binário antes
e depois. Conferir igualdade de bytes e ausência de `security.capability` na
imagem final. Executar `caddy version` em fixture sem rede, UID 1000, rootfs RO,
zero capabilities e `no-new-privileges`, depois validar a configuração pública
do qbit-monitor sob a mesma política. O default da imagem é somente `version`.
Esses checks reais pertencem ao operador; os testes locais de filesystem não
afirmam build ou execução Docker concluídos.

As camadas e metadados herdados da base continuam privados. `env -i` é mantido
em cada RUN e no entrypoint final para não entregar ambiente capturado ao
processo. Não publicar imagem ou logs brutos de inventário.

# Preparação do checkpoint operacional

Inspeção somente leitura em **15/11/2026, 12:58–13:03 em São Paulo**
(15:58–16:03 UTC). Este documento fecha o levantamento e o procedimento proposto;
**a captura consistente e sua restauração ainda não foram executadas**.
O manifesto privado mantém `capture_allowed=false`, `restore_allowed=false`
e `production_ready=false` enquanto as condições abaixo não forem atendidas.
Não foi instalado um gerenciador de backups, agenda ou unit do host.

## Resultado da inspeção

| Item | Evidência observada | Consequência |
|---|---|---|
| Projeto de mídia | 30 containers: 21 ativos e 9 parados | Retorno pelos IDs originalmente ativos; não iniciar os parados |
| Outros containers | 6: quatro fixtures antigas paradas e outro projeto com um container ativo e um parado | O checkpoint de mídia não basta para reinstalar o SSD do servidor inteiro |
| Imagens da mídia | 20 imagens locais consultáveis, aproximadamente 17,93 GB pela soma de `Size`; uma referência indisponível usada por três workers parados | Preservar as 20 imagens presentes relacionadas no manifesto; não declarar reconstrução dos 30 containers em outro host |
| Persistência adicional | Um volume anônimo no `/cache` do Jellyfin | Incluir conteúdo e mapa do volume; não excluir por se chamar cache |
| Configuração efetiva | Listas de Compose diferentes entre serviços, com overrides privados externos ao checkout | Preservar o mapa por container; não substituir por `compose up` genérico |
| Camadas graváveis | `docker diff` consultado nos 30 containers | Há mudanças fora dos binds; guardar só appdata e imagens originais não demonstra reconstrução fiel |
| Recuperação USB | Estado publicado `ready`, com evento presente | Não foi invocado o comando de status, nem alterada a recuperação; isso não prova retorno físico do HD |
| Units antigas do HomeServer | As consultadas estavam inativas; timers desabilitados | Preservar configuração e estado observado, sem reativar componentes retirados |

O censo usou metadados de arquivos, sem abrir mídia, bancos ou configurações.
Somente campos permitidos dos JSONs operacionais foram lidos. Não foram
capturados valores de `.env`, variáveis dos containers ou argumentos de processos.
Inventários, nomes de mídia, identidades, caminhos privados e dados de conexão
ficam fora do Git. Os endpoints HTTP não foram retestados nesta rodada.

### Verificação do acervo

O relatório existente registra **6.029/6.029 grupos saudáveis**, 8.215 caminhos,
zero erros e `complete_healthy=true`. O PID registrado pelo verificador já não
existia na consulta. Isso é leitura do relatório, sem reautenticar hashes nem
provar que nenhum outro escritor possui locks.

Continua **uma pendência de reparo**, `content_verified=false` e pipeline em
`hold`, com `published=false` no relatório consultado. O progresso antigo de
reparo indica espera por verificação offline; não houve novo reparo nesta etapa.
Não é necessário iniciar outro verificador dos grupos saudáveis para preparar
o checkpoint. Para publicar mídia, ainda são necessárias prova atual de conteúdo,
reconciliação do reparo, delta e estado coerente por lote.

## Escopo e capacidade

O manifesto privado relaciona containers, imagens, binds, volume, arquivos
individuais e identidades das raízes. O censo terminou as dez raízes declaradas
(sete raízes principais e três diretórios de overrides de units), sem erros ou
mounts aninhados encontrados. Isso mede o conjunto declarado; não demonstra
que todas as dependências externas já foram resolvidas.

| Conjunto | Bytes lógicos únicos, aproximados |
|---|---:|
| `/srv/appdata`, incluindo controle, bancos, sidecars e reparo | 32,31 GB |
| Checkout efetivo, incluindo estado histórico local | 1,97 GB |
| Estado e overrides externos da operação | 23,37 GB |
| Estado e incidentes da recuperação USB | 5,31 GB |
| Volume anônimo Jellyfin | 0,27 GB |
| Instalação/configuração do host e arquivos individuais | Inventariados separadamente; complemento pequeno |
| Total das raízes, deduplicando inodes | **63,23 GB** |

Há aproximadamente **20,27 GB de arquivos de reparo dentro do appdata**.
São parte do escopo conservador: não excluir como temporários nem confundir
com a biblioteca publicada. Nenhum conteúdo desses arquivos foi lido ou copiado.
O levantamento também encontrou sockets, FIFOs históricos e links absolutos.
Eles precisam de tratamento explícito no manifesto antes da captura; não usar
exclusões silenciosas ou seguir links para ampliar o escopo.
São 259.322 membros candidatos, quatro entradas especiais e 989 links que
exigem revisão por serem absolutos ou apontarem fora dos membros inventariados.
Em 1.063 grupos de hardlinks, a contagem de nomes no escopo difere de `nlink`:
existem aliases não cobertos ou mudanças concorrentes a reconciliar. Isso não
autoriza incluir árvores externas automaticamente nem declarar a topologia completa.

| Destino | Espaço disponível observado | Uso proposto |
|---|---:|---|
| HD de 18 TB | 6,84 TB | Bundle privado, fora da biblioteca, staging e raízes capturadas |
| Windows, NTFS | 657,70 GB | Segunda cópia opaca, com ACL restrita |
| Linux do servidor, `/` e `/tmp` | 313,54 GB compartilhados | Não usar como destino principal do ensaio |
| WSL, ext4 | 1.012,55 GB reportados pelo filesystem virtual | Restauração e cópia descartável para validação |

**O WSL consome o mesmo espaço físico do Windows.** Os números não se somam.
Reservar provisoriamente 120 GB para cada cópia do bundle; no computador local,
o teto de planejamento é 480 GB para cópia Windows, retorno ao WSL, duas árvores
Linux, imagens e margem. Restam aproximadamente 178 GB frente à medição atual.
Essa reserva comporta o conjunto medido, mas precisa ser recalculada após resolver
camadas graváveis e qualquer ampliação do escopo. `Size` das imagens é estimativa,
não tamanho medido de `docker save`. Conferir espaço e inodes novamente na janela.
Os três volumes menores de mídia têm apenas cerca de 11 GB livres cada; não são
destinos do checkpoint.

## Condições para capturar

1. **Preservação completa:** classificar mudanças das camadas graváveis, links,
   sockets e FIFOs. A captura de imagens originais não inclui alterações da camada
   gravável nem volumes. Escolher e ensaiar a preservação dessas alterações antes
   de declarar reconstrução em outro host. Metadados online não comprovam ACLs,
   xattrs, integridade dos bancos ou conteúdo.
2. **Lista exata:** atualizar o manifesto e, já sob quiescência, enumerar todo o
   conjunto novamente e gerar `members.nul` com cada membro
   e diretório necessário, nomes relativos a `/`, sem `..`, duplicatas ou destinos
   de backup. Não há lista pronta liberada para captura nesta entrega. Incluir
   `.env`, todos os overrides, arquivos individuais e dados de reparo; nenhum
   diretório desconhecido pode ser omitido para fazer a captura passar.
3. **Tratamento de links:** preservar o link, nunca desreferenciá-lo na captura.
   Links externos/absolutos devem ter destino e uso classificados. Mantê-los
   inertes durante o ensaio; não iniciar aplicativos que possam sair da árvore
   restaurada. Comparar destinos textuais sem seguir links; a classificação
   não comprova funcionamento da dependência. Conferir todos os aliases de
   hardlinks, inclusive entre raízes.
4. **Quiescência:** fechar entradas e automações, controlar a recuperação USB,
   concluir ciclos e parar somente os IDs previstos, conforme o
   [roteiro de contenção](media-precutover-review.md). Conferir escritores do host,
   clientes diretos e incidentes; Docker parado sozinho não comprova quiescência.
   O marcador de manutenção existente conserva seu dono. Não remover locks.
5. **Janela:** o proprietário ainda precisa liberar a indisponibilidade. A
   conclusão global da mídia não é requisito automático para capturar aplicações
   sem mudar seu layout; escritores sobrepostos e estado de reparo precisam estar
   estáveis. A migração posterior mantém requisitos adicionais de conteúdo.

Os arrays privados de parada/retorno devem particionar exatamente os 21 IDs
ativos recapturados. Os nove parados da mídia e os outros projetos não entram
nesses arrays. Se aparecer novo container, restart, mount ou escritor, refazer
o plano antes da parada. Nenhuma ação abaixo está liberada só por copiar comandos.

## Captura e retorno propostos para a janela

O procedimento é manual e usa `state.tar`, `images.tar`, manifesto de conteúdo,
metadados e recibos. A [fixture nativa](native-checkpoint-rehearsal.md) possui
limites de 4 GiB por arquivo, 8 GiB por bundle e 100 mil membros; o conjunto real
excede esses limites e inclui tipos/links não aceitos pela fixture. Não executá-la
contra produção nem aumentar limites para aparentar validação operacional.

Depois de todas as condições anteriores, usar diretórios privados novos,
fora das fontes. As variáveis abaixo representam caminhos e IDs do manifesto
recapturado; não são defaults de produção. A lista NUL explícita impede recursão
para conteúdo novo durante a captura:

```bash
sudo env -u TAR_OPTIONS tar \
  --create --format=pax --numeric-owner \
  --acls --xattrs --xattrs-include='*' --sparse --check-links \
  --no-recursion --null --verbatim-files-from --directory=/ \
  --files-from="${HS_PRIVATE:?}/members.nul" \
  --file="${HS_BUNDLE:?}/state.tar.partial" \
  2>"${HS_PRIVATE:?}/state-tar.stderr"

: "${HS_IMAGE_IDS[0]:?lista de imagens vazia}"
sudo docker image save --output "${HS_BUNDLE:?}/images.tar.partial" \
  "${HS_IMAGE_IDS[@]}"
```

São dois passos independentes: **não avançar se o anterior falhar**. Registrar
exit codes e revisar todos os avisos, inclusive hardlinks ausentes ou arquivos
alterados. Não usar opções para ignorar erros, `--dereference` ou
`--hard-dereference`. Reenumerar o conjunto completo depois da captura para
detectar também membros novos fora da lista; revalidar conteúdo e metadados sob
a mesma contenção. Gerar o manifesto de hashes dos arquivos e metadados nessa
captura, não a partir do censo online. Sincronizar os arquivos, promover `.partial`
e sincronizar o diretório após os renames, antes de emitir recibo.
Só então calcular SHA-256/tamanho finais,
guardar o esperado separadamente e transferir os arquivos opacos para o Windows.
Reler o hash no HD e no Windows. Não extrair no NTFS.

O GNU tar documenta [listas NUL](https://www.gnu.org/software/tar/manual/html_section/files.html)
e [preservação de atributos](https://www.gnu.org/software/tar/manual/html_section/create-options.html).
O [Docker save](https://docs.docker.com/reference/cli/docker/image/save/) exporta
imagens; não substitui a captura dos volumes ou a resolução das camadas graváveis.

No ensaio, transferir a cópia Windows de volta ao ext4 do WSL e conferir hashes
antes de extrair. Validar o manifesto e os membros do arquivo antes da extração,
incluindo caminhos, colisões e ancestrais symlink. Usar raiz nova sob pai privado
0700; nunca extrair sobre `/`, appdata real ou DockerRootDir:

```bash
sudo env -u TAR_OPTIONS tar \
  --extract --numeric-owner --same-owner --same-permissions \
  --acls --xattrs --xattrs-include='*' --delay-directory-restore \
  --file="${HS_RETURN:?}/state.tar" --directory="${HS_RESTORE:?}"
```

Conferir membros, hashes, UID/GID, modos, mtime, ACLs, xattrs, links e topologia
dos hardlinks. Erros de suporte a atributos bloqueiam o aceite. Manter essa
referência intacta; verificar SQLite com sidecars e demais dados em outra cópia
descartável. `tar --compare` ou hashes do bundle sozinhos não demonstram tudo isso.
Carregar as imagens em daemon de ensaio isolado e validar cada serviço em cópias
de trabalho sem rede externa, dispositivos ou binds reais. O volume Jellyfin
precisa de volume/diretório de ensaio próprio. Não há ensaio desses 21 serviços
de produção aprovado por esta inspeção. Dependências externas não resolvidas
bloqueiam iniciar clones. Um aplicativo que iniciou sem a biblioteca esperada
e alterou seus registros não comprova preservação do catálogo.

Sem mudanças em produção, o retorno usa `docker start` nos mesmos IDs originalmente
ativos, em ordem revisada: infraestrutura/telemetria, consumidores nativos sob
contenção, API/gateway, entradas e worker por último. Preservar manutenção,
admissão e holds; iniciar qBittorrent pode retomar tarefas, portanto a contenção
nativa precisa ser comprovada antes. Revalidar estado/catálogo/filas e compatibilidade
do evento de recuperação antes de reabilitá-la. Não usar `up`, recreate ou iniciar
one-offs. Retomar esses containers não restaura bancos byte a byte.

Se o ensaio falhar, manter originais e evidência, sem restaurar por cima de produção.
Após qualquer nova escrita, um rollback de banco requer reconciliação do delta.
O checkpoint não autoriza formatação, substituição de SSD, mudanças de mounts,
publicação ou cleanup de mídia. A reinstalação geral depende também da preservação
do outro projeto encontrado e do plano da plataforma.

## Evidências e próximo fechamento

As evidências privadas incluem inventário completo do Docker, censo das raízes,
capacidade, progresso existente e manifesto com cobertura de binds e pendências.
Os coletores não foram instalados no servidor. O pacote de planejamento separa
`inventory_complete_for_declared_roots` de prontidão para captura/restauração.

O próximo fechamento técnico é resolver as camadas graváveis e os membros especiais,
comprovar contenção real e preparar a lista de captura validada. Depois da liberação
da janela, executar captura e restauração conforme os critérios acima. A migração
incremental do acervo continua dependente do reparo, prova atual e executor operacional.

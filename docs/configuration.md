# Configuração

Copie [`.env.example`](../.env.example) para `.env`. Ele contém preferências
editáveis e credenciais da instalação. Caminhos, portas, UID/GID e URLs internas
são defaults do código/Compose, para que a operação cotidiana se concentre no
comportamento do servidor. Não há arquivos de segredo `*_FILE`.

O catálogo tipado em `homeserver_common.settings` é a referência para nomes,
defaults, tipos e unidades. Consulte-o sem revelar credenciais:

```bash
docker compose run --rm operator config catalog
docker compose run --rm operator config validate --env-file /project/.env
docker compose run --rm operator config show --redacted --env-file /project/.env
```

## Sintaxe e aplicação

Cada linha contém `HOMESERVER_CHAVE=value`. Linhas vazias e comentários iniciados
por `#` são ignorados. O loader trata o conteúdo como dados: não expande `$`,
não executa comandos e não interpreta comentários ao final de valores. Uma
chave duplicada ou desconhecida é erro de validação.

Valores sem aspas perdem espaços externos; aspas simples delimitam texto
literal. Aspas duplas usam escapes JSON. O exemplo usa strings JSON:

```dotenv
HOMESERVER_DOWNLOAD_DEFAULT_PROFILE="Standard"
HOMESERVER_RELEASE_MIN_SEEDERS="5"
HOMESERVER_DUBBED_AUDIO_PREFERENCES="dual,original"
HOMESERVER_ADMIN_PASSWORD="preencha sua senha"
HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=""
```

Não execute `source .env` nem `eval`. Guarde o arquivo com permissão `0600`;
ele está ignorado pelo Git. Os consumidores rodam UID 1000 e precisam ler o
arquivo; preserve esse owner ou adapte a identidade estrutural no Compose.
O operator escreve API keys adotadas no mesmo
arquivo, sem criar arquivos de segredo separados. Veja
[o procedimento de alteração](operator-guide.md#aplicar-uma-mudança).
Radarr e Sonarr recebem, cada um, os perfis `Max Quality`, `Standard` e `Dublado`.
O Seerr usa `DOWNLOAD_DEFAULT_PROFILE`, cujo padrão é `Standard`, nas conexões
de filmes e séries. O perfil escolhido no pedido ou no cadastro do Arr orienta
as próximas buscas daquele título; o gênero não sobrescreve essa escolha.
Na migração, cadastros no antigo `HomeServer Animation` passam a `Dublado` e
os demais perfis obsoletos passam ao padrão configurado, inicialmente `Standard`.
O apply reassocia os cadastros
antes de remover as definições antigas, preservando arquivos e torrents.

## Downloads e seeding

`HOMESERVER_STORAGE_MIN_FREE_GIB="10"` mantém uma margem de 10 GiB por disco
na admissão, renovação e retomada de downloads. Aceita inteiro maior ou igual
a zero. SSD e HD descontam sua própria margem depois dos compromissos restantes
da fila, inclusive fontes paradas. O valor não altera a reserva do filesystem
nem os números de espaço físico do painel. Aplique a mudança recriando worker
e gateway para que os dois usem a mesma configuração.

| Parâmetro, com prefixo `HOMESERVER_` | Default | Unidade e efeito |
|---|---|---|
| `DOWNLOAD_MAX_ACTIVE` | 10 | Downloads simultâneos globais |
| `SERIES_DOWNLOAD_WINDOW` | 10 | Janela de transferências por série; pack usa uma vaga |
| `SEED_MAX_ACTIVE` | -1 | Torrents em seeding; -1 deixa a quantidade ilimitada |
| `TORRENT_MAX_ACTIVE` | -1 | Total ativo nativo qBit; -1 deixa a quantidade ilimitada |
| `QUEUE_IGNORE_SLOW_TORRENTS` | false | Downloads lentos também contam no limite global |
| `UPLOAD_LIMIT_MBIT` | 20 | Mbit/s; zero é ilimitado |
| `DOWNLOAD_LIMIT_MBIT` | 0 | Mbit/s; zero é ilimitado |
| `TORRENT_MAX_CONNECTIONS` | 500 | Conexões globais |
| `TORRENT_MAX_CONNECTIONS_PER_TORRENT` | 100 | Conexões por torrent |
| `UPLOAD_SLOTS`, `UPLOAD_SLOTS_PER_TORRENT` | 20, 4 | Slots de upload |
| `SEED_RATIO_LIMIT` | -1 | Ratio; -1 desativa o limite |
| `SEED_TIME_LIMIT_MINUTES`, `SEED_INACTIVE_LIMIT_MINUTES` | -1, -1 | Minutos; -1 desativa |

Mbit/s é decimal: 20 Mbit/s equivale a 2.500.000 bytes/s. qBit pode arredondar
para KiB inteiros; o read-back mostra o valor efetivo. O total ativo deve
comportar os limites individuais; se seeding é ilimitado, o total também deve
ser ilimitado. Os limites de ratio/tempo ficam desligados por padrão: a banda
de upload limita o envio, sem restringir a quantidade de arquivos disponíveis.
Contribuir com seeding depende de upload,
peers e disco disponíveis, não somente do limite configurado.

Capacidade considera tamanho solicitado, bytes já baixados, bytes ainda
pendentes e reservas concorrentes no filesystem de mídia. Não existe reserva
fixa de 80 GB por filme nem teto estático de tamanho.

## Qualidade e fontes lentas

Os três perfis têm as mesmas regras para filmes e séries:

| Perfil | Qualidades aceitas, em ordem de preferência |
|---|---|
| `Max Quality` | UHD Remux → UHD Blu-ray → UHD WEB-DL/BDRip/BRRip → FHD Remux → FHD Blu-ray → FHD WEB-DL/BDRip/BRRip |
| `Standard` | FHD Blu-ray → FHD WEB-DL/BDRip/BRRip |
| `Dublado` | FHD Blu-ray → FHD WEB-DL/BDRip/BRRip, depois de comparar a prioridade de áudio |

UHD corresponde a 2160p e FHD a 1080p. Standard e Dublado não aceitam Remux
nem UHD; WEBRip e TV ficam excluídos dos três perfis. WEB-DL, BDRip e BRRip
empatam no mesmo nível visual; o controlador distingue rips pelo título e os
perfis nativos usam grupos e Custom Formats gerenciados. No Dublado, dual
PT-BR + original precede original. Assim, um WEB-DL dual pode vencer um Blu-ray
com áudio original. Dublagem sem original fica excluída; um filme originalmente
brasileiro continua elegível como original. Essa escolha vale também para conteúdo que não é
animação. Selecione o perfil nas opções avançadas do pedido do Seerr ou edite o
campo de perfil de qualidade do filme no Radarr ou da série no Sonarr.
O Sonarr mantém um perfil por série, compartilhado por todas as temporadas,
inclusive quando há pedidos e downloads de temporadas diferentes ao mesmo tempo.

| Parâmetro | Default | Efeito |
|---|---|---|
| `DOWNLOAD_DEFAULT_PROFILE` | Standard | Perfil padrão no Seerr para novos pedidos de filmes e séries |
| `RELEASE_MIN_SEEDERS` | 5 | Mínimo de seeds declarados para uma candidata ser elegível em qualquer perfil |
| `DUBBED_AUDIO_PREFERENCES` | dual,original | Ordem de áudio alinhada ao perfil Dublado, antes da qualidade visual |
| `RELEASE_INDEXER_PRIORITY` | uindex,1337x | Fontes nativas habilitadas; aceita também amigosshare quando configurado |
| `RELEASE_FALLBACK_INDEXERS` | vazio | Opt-in: fontes habilitadas inspecionadas somente após esgotar as demais; `1337x` preserva UIndex/ASC no primeiro grupo |
| `INDEXER_FALLBACK_MIN_SEEDERS` | 5 | Mínimo para preferir pacote de temporada e manter afinidade de releases |
| `MOVIE_INDEXER_FALLBACK_MIN_SEEDERS` | 20 | Preferência legada de fallback; não impede comparar ambos os indexadores |
| `SERIES_PREFER_SEASON_PACK` | true | Compatibilidade; temporada encerrada exige pack completo mesmo se false |
| `SERIES_RELEASE_AFFINITY` | true | Favorecer o mesmo indexador e família de releases saudáveis na série |
| `PREFER_DOLBY_VISION`, `PREFER_ATMOS` | true, true | Preferências de vídeo/áudio |
| `QUALITY_MIN_MIB_PER_MIN_720` | 0 | Sem piso de tamanho; valor positivo habilita piso opcional |
| `QUALITY_MIN_MIB_PER_MIN_1080` | 0 | Sem piso de tamanho; valor positivo habilita piso opcional |
| `QUALITY_MIN_MIB_PER_MIN_2160` | 0 | Sem piso de tamanho; valor positivo habilita piso opcional |
| `AUDITED_IMPORT_ALLOW_SEERR` | false | Com manifesto ativo, permite pedidos novos junto da importação auditada |
| `AUTOMATIC_UPGRADES` | false | Atualizações automáticas após aquisição |
| `SOURCE_AUTOMATIC_REPLACEMENT_ENABLED` | false | Mantém a fonte até concluir ou até uma troca manual pelo painel |
| `SOURCE_SLOW_REPLACEMENT_ENABLED` | false | Quando a troca automática estiver habilitada, permite também considerar lentidão |
| `SOURCE_SLOW_WINDOW_SECONDS`, `SOURCE_STALL_SECONDS` | 300, 300 | Janela de lentidão ou falta de progresso |
| `SOURCE_MIN_RATE_KIB` | 1024 | KiB/s; limiar de lentidão |
| `SOURCE_PROBE_SECONDS` | 60 | Medição da candidata |
| `SOURCE_MIN_TIME_GAIN_PERCENT` | 20 | Melhoria mínima do ETA medido |

A seleção compara as releases elegíveis do UIndex e do 1337x antes de iniciar
o download. Seeds zero, desconhecidos ou abaixo de `RELEASE_MIN_SEEDERS`
impedem uma nova aquisição. Entre as candidatas viáveis, a ordem de qualidade
do perfil precede a quantidade de seeds; no Dublado, o áudio precede também
a qualidade. Dolby Vision/Atmos continuam como preferências de desempate.
Por padrão não há piso MiB/min. Filmes elegíveis podem passar à frente por seeds.
Séries podem baixar em paralelo dentro de `SERIES_DOWNLOAD_WINDOW`, respeitando
o limite global. A janela contém os primeiros episódios ainda não baixados,
contando cada torrent de temporada como uma transferência:
um episódio sem fonte ou lento não impede buscar os seguintes dessa janela.
Um torrent confirmado com zero bytes restantes no qBit libera sua vaga mesmo
quando aguarda a importação. A janela avança para os próximos episódios e pode
alcançar as temporadas seguintes já solicitadas. Sem evidência atual do qBit,
o episódio continua ocupando a vaga.
Os episódios vinculados ao mesmo parent de pack compartilham uma vaga; os
avulsos, inclusive os que ainda não têm fonte, ocupam uma vaga cada.
Importação/Jellyfin mantém temporada/episódio em ordem: E7 pronto espera E5/E6;
S2 pode baixar quando couber na janela, mas aguarda S1 para ser importada.
Episódios desmonitorados ou futuros conhecidos não bloqueiam a cadeia.
Um episódio completo aguardando importação não conta como download ativo.
Seeds anunciados não garantem velocidade real.

Uma temporada comprovadamente encerrada exige um pack completo dessa única
temporada para novas aquisições, mesmo se um avulso tiver qualidade, áudio ou
seeds melhores. Os perfis comparam qualidade e áudio entre os packs elegíveis;
o fallback de áudio permanece entre packs. Temporadas em andamento permitem
avulsos já lançados. Fontes admitidas e episódios importados são preservados.

A decisão usa todo o catálogo canônico do Sonarr, incluindo episódios não
monitorados. Uma data futura conhecida indica temporada em andamento. Para
confirmar encerramento, todos os episódios precisam ter datas UTC já passadas,
numeração íntegra e evidência independente: série encerrada, finaleType de final
de temporada/série já lançado ou episódio de temporada posterior já lançado.
Ter apenas todos os episódios conhecidos no passado não confirma encerramento.
Datas desconhecidas, lacunas ou contagens divergentes aguardam metadados.
Especiais S00 não entram nessa decisão. EP/SE só são resolvidos contra o catálogo
canônico; números absolutos de anime exigem mapeamento único na série inteira.
Packs com várias temporadas ou episódios combinados continuam recusados.

Séries favorecem a mesma família (grupo de release e origem, como AMZN/DSNP)
depois de comparar seeds, quando ela tem seeds suficientes; a afinidade não
prende a série a uma fonte fraca. Um pacote precisa conter todos os episódios esperados da temporada,
com um vídeo válido por episódio e piso de qualidade individual. É uma única
transferência e uma única reserva do tamanho total, incluindo episódios já
baixados separadamente se o pacote os contém. Os vínculos desses episódios
preservam os arquivos existentes. Cada importação aponta somente ao arquivo
do próximo episódio, mantendo a ordem mesmo quando o pacote baixa em paralelo.
Pacotes com caminhos que colidem com uma fonte preservada são recusados;
nesses casos a temporada encerrada aguarda outro pack elegível.
Ao excluir um episódio pelo Jellyfin, o fluxo remove sua entrada e arquivo
da biblioteca e dos serviços. Também é possível excluir uma temporada inteira
no menu da temporada: o controlador valida os episódios locais, registra a
operação durável e desmonitora somente a temporada selecionada, cancelando
suas fontes gerenciadas pendentes. Outras temporadas são preservadas.
Um pacote compartilhado permanece no qBit enquanto serve outros episódios;
o espaço físico dessa fonte só é liberado depois que todos os episódios
vinculados forem explicitamente excluídos. Nesse momento o gateway revalida
o pacote e remove seu torrent e arquivo. Arquivos compartilhados por vários
episódios ou identidades ambíguas bloqueiam a operação.
Pedidos do Seerr que abrangem outras temporadas permanecem; só pedidos
exclusivos da temporada selecionada são removidos. A atualização dos catálogos
é assíncrona. Marcar como assistido nunca inicia exclusão. Consulte
[o procedimento de exclusão](operator-guide.md#remover-mídia-assistida).

Magnets têm seus trackers incorporados aos metadados verificados antes do
envio ao qBit. O conteúdo identificado pelo infohash permanece igual.

O tamanho real dos arquivos é usado para reservar espaço; por padrão não há
reprovação baseada em MiB por minuto. Se um piso positivo for configurado,
ele compara bytes do vídeo principal e duração declarada, sem somar samples
ou sidecars. Zero desativa esse piso; uma duração desconhecida só bloqueia
a regra opcional quando ela estiver habilitada. Isso vale também para
candidatas de failover, sem apagar arquivos existentes.

Com `SOURCE_AUTOMATIC_REPLACEMENT_ENABLED=false`, nem lentidão nem ausência de
peers/progresso disparam troca. O card **Downloads** no painel permite solicitar
outra fonte explicitamente; a busca mantém a atual enquanto não encontra uma
alternativa compatível que caiba no disco.
Se a troca automática e `SOURCE_SLOW_REPLACEMENT_ENABLED` forem habilitadas,
após cinco minutos lento o worker busca e mede uma candidata
mantendo o download atual. Promoção exige qualidade/edição compatíveis,
capacidade conjunta e previsão de término melhor. Pausas por operador/capacidade
e torrents completos não disparam troca. Ausência sustentada de progresso
fica sujeita à recuperação após `SOURCE_STALL_SECONDS` somente com a troca
automática habilitada. Alterações da política orientam
seleções futuras; não autorizam cancelar aquisições em andamento.
Essa recuperação automática atende torrents individuais. Um pacote de temporada
já admitido ainda requer intervenção se sua fonte ficar sem progresso.

## Idiomas de áudio e legenda

`AUDIO_LANGUAGES` aceita lista ordenada, por exemplo `original,pt-BR,en-US`.
Ela considera somente idiomas declarados nos metadados da release.
`original` exige contexto de idioma original do filme/série fornecido pelo Arr.
Sem contexto ou metadados, não há pontuação de idioma; a release continua
elegível pelos demais critérios. Nome do arquivo, título e ID numérico isolado
não comprovam idioma. `pt` genérico não comprova a região brasileira.

`DUBBED_AUDIO_PREFERENCES="dual,original"` mantém a ordem
para filmes e séries com o perfil `Dublado`:

1. `dual`: dual/multi com pt-BR e um idioma original distinto declarados.
2. `original`: idioma original conhecido do filme ou da série, sem PT-BR adicional.

Escolher `Dublado` ativa essa preferência, qualquer que seja o gênero. Os perfis
`Max Quality` e `Standard` preferem original e depois dual PT-BR + original,
após comparar a qualidade. Áudio explícito sem o idioma original é recusado;
fontes sem declaração de áudio nesses dois perfis ficam na última prioridade,
com idioma inconclusivo. As regras dos perfis nomeados não iniciam upgrades ou trocas
automáticas de arquivos existentes. Animações novas não recebem uma troca
automática de perfil. O gênero explícito `Animation`, `Animação` ou `Anime`
continua relevante para a política de legendas descrita abaixo.

No Radarr, os três perfis usam idioma nativo `Any`; o controlador exige original
ou dual PT-BR + original conforme o perfil. Os Custom Formats ordenam a qualidade
e o áudio. Uma condição de título reconhece a declaração dual do ASC quando o
parser nativo identifica apenas PT-BR; o controlador só confia nessa declaração
para a fonte ASC. O Sonarr também oferece os três perfis
e a mesma política de seleção para séries.

No Dublado, áudio vem antes da qualidade visual e dos seeds entre fontes
elegíveis. Por exemplo, FHD WEB-DL dual pode vencer FHD Blu-ray somente no
idioma original. Dentro da mesma prioridade de áudio, Blu-ray precede WEB-DL;
depois são comparados seeds e os demais desempates. Os filtros de edição,
tamanho e espaço continuam obrigatórios, assim como o mínimo configurado em
`RELEASE_MIN_SEEDERS`. Para temporadas encerradas, esses critérios comparam
somente packs completos da temporada. Sem pack elegível ou com espaço insuficiente,
o controlador espera; não substitui o pack obrigatório por episódios avulsos.

Declarações explícitas de idioma nos metadados ou marcações inequívocas de áudio
da release podem orientar essa preferência. `Dual`, `Multi` ou português genérico
sozinhos não comprovam a combinação pt-BR + original. Um título que contenha
`Brazil` também não comprova áudio brasileiro. Sem declaração suficiente, a
release é pulada até existir uma candidata classificável em uma das prioridades.
Idioma original desconhecido não é presumido como inglês: uma release só em
inglês, por exemplo, não satisfaz `original` nesse caso. Uma declaração explícita
de pt-BR ainda pode satisfazer a prioridade de dublagem, mesmo sem contexto
do idioma original. Isso não confirma a combinação dual pt-BR + original.

A descoberta Dublado complementa a busca nativa com o título principal,
original e variantes brasileiras disponíveis no cadastro do Arr. Consulta
somente UIndex e 1337x, também com `DUAL`, `DUBLADO` e `PTBR`; temporadas usam
o identificador `Sxx`. A consulta complementar inclui categorias que podem estar
erradas no anúncio. Cada resultado novo precisa ser identificado pelo parser
nativo: filme e ano exatos, ou série, temporada e episódios correspondentes.
Resultados repetidos são unidos pelo hash/GUID; uma rejeição nativa existente
não é apagada por uma cópia encontrada na busca complementar.

O idioma que o Radarr preenche a partir do cadastro do filme não comprova áudio
da release. Dublado usa as marcações de áudio do título, os idiomas explícitos
do parser e a seção de áudio da descrição/MediaInfo quando disponível.
`SUBS` não apaga o áudio declarado em outra parte do título, e nomes entre
colchetes, como `DUAL AUDIO [PTBR+ENG]`, são interpretados. Legendas não contam
como faixas de áudio. Português genérico continua sem comprovar a região.

As consultas são limitadas e reutilizam cache: até 12 buscas complementares,
30 novos parses e 8 novas descrições por execução, com intervalo entre buscas
externas. Cada descoberta tem orçamento total de 30 segundos (até 20 para
consultas), para não segurar o ciclo de importação por vários minutos. Consultas
individuais têm limite de 15 segundos e descrições, 12 segundos. A próxima
execução continua pelas consultas restantes; timeout aguarda 300 segundos antes
de repetir a mesma consulta. Candidatos explicitamente PT-BR/dublados são inspecionados antes de
`DUAL/MULTI` sem idiomas. Inspeções já feitas liberam o limite para os próximos
candidatos nos ciclos seguintes. Bloqueio, erro ou inspeção ainda incompleta
gera `search_incomplete`: uma fonte com PT-BR comprovado pode seguir, mas o
fallback apenas original aguarda a conclusão da busca.

Uma fonte complementar só é enviada ao `release/push` do Arr depois de passar
pelos filtros do controlador, inspeção do torrent e reserva de capacidade real.
O Arr aplica novamente suas decisões nativas antes de baixar. Uma rejeição
cancela somente a autorização ainda não usada; mídia e fontes existentes são
preservadas. Downloads iniciados não são substituídos por esta melhoria.

Ao concluir, o `ffprobe` compara as faixas do vídeo com a preferência registrada
na escolha. Comentários não contam como áudio principal. Ausência de tags ou
português sem região gera resultado inconclusivo; uma divergência gera aviso
nos logs. Essa auditoria não apaga, troca nem impede a importação do arquivo.

`SUBTITLE_LANGUAGES` aceita `pt-BR,en-US`. Com
`SUBTITLE_MATCH_MODES=release,compatible_edition`, a ordem padrão é:

1. pt-BR da mesma release.
2. pt-BR de edição compatível.
3. Inglês da mesma release.
4. Inglês de edição compatível.

O filtro de edição evita legendas de extended/director cut em versão base,
usando metadados e cobertura temporal, com
`SUBTITLE_CREDITS_MARGIN_SECONDS=600` por padrão. Não exige o mesmo nome de
release e não garante sincronismo perfeito. `SUBTITLE_ALLOW_GENERIC_ENGLISH=true`
aceita inglês genérico quando o provedor não declara região. pt-PT não é pt-BR.

Depois da importação, o finalizador também reconhece a legenda válida instalada
pelo Bazarr junto do vídeo. Confere idioma, conteúdo e pool físico antes de
concluir; preserva o arquivo e a prioridade pt-BR. Uma legenda somente forçada
não substitui a legenda completa exigida pelo perfil.

`JELLYFIN_SUBTITLE_LABELS_ENABLED=true` padroniza o seletor dos clientes que
acessam o proxy do projeto. `JELLYFIN_SUBTITLE_LABEL_EN_US="English - US"` e
`JELLYFIN_SUBTITLE_LABEL_PT_BR="Português BR"` definem os nomes. Inglês sem região
usa o nome configurado; evidências de outra região são preservadas. Português
sem evidência de Brasil permanece `Português`; pt-PT aparece como `Português PT`.
Faixas forçadas, SDH, padrão e títulos de fontes distintas continuam identificados.
Somente `DisplayTitle` muda nas respostas de catálogo, reprodução e notificações:
idioma, índice e conteúdo dos arquivos permanecem iguais. Conexões internas
diretas à API nativa não passam por essa apresentação.

`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` aceita somente `pt-BR` nesta versão.
Dispensa exige evidência de áudio original brasileiro na mídia; dublagem ou
português genérico não bastam. Valor vazio desativa a dispensa. O perfil Dublado
e as animações não usam essa dispensa: todas as prioridades de áudio, inclusive original pt-BR,
seguem a mesma busca pt-BR/inglês descrita acima. O perfil gerenciado do Bazarr
mantém `áudio_exclude=false`, para que ter áudio em português não exclua
a mídia da busca de legendas.

Séries exigem legenda antes da publicação por padrão. O proprietário pode
autorizar uma exceção por título para publicar o vídeo enquanto a recuperação
continua. A exceção preserva as validações de arquivo, identidade, capacidade
e ordem dos episódios. A pendência de legenda permanece registrada por episódio
e continua sendo tentada após reinícios ou depois de o Seerr indicar disponibilidade.

Quando o SubDL responde com limite de uso, o worker respeita `Retry-After`;
sem prazo válido, aguarda 15 minutos antes de consultar novamente pela mesma
instância do provedor. Essa pausa não significa que a legenda não exista.

## Indexadores, provedores e credenciais

`PROWLARR_INDEXERS` começa como `[]`. Cadastre UIndex e 1337x com suas
definições nativas conforme [o guia dos serviços](runbooks/service-setup.md).
`RELEASE_INDEXER_PRIORITY` limita tanto a escolha do worker quanto os
indexadores habilitados no Prowlarr/Arr; os demais ficam desativados, sem apagar
suas configurações. Qualidades e resoluções são definidas pelo perfil escolhido
para cada título.

Para usar 1337x somente após esgotar as candidatas elegíveis de UIndex/ASC,
defina `HOMESERVER_RELEASE_FALLBACK_INDEXERS="1337x"`. A lista deve ser um
subconjunto das fontes habilitadas e deixar ao menos uma fonte no primeiro
grupo. Vazio preserva a comparação legada. Qualidade, áudio, mínimo de seeds,
packs completos, gateway e capacidade continuam sendo exigidos; uma recusa
de admissão permite tentar as demais candidatas antes de passar ao fallback.

O Amigos Share Club é opcional. Use a definição nativa `amigosshare`, com
`implementation: Cardigann`, URL `https://amigos-share.club/` e campo `cookie`.
Guarde essa entrada em `HOMESERVER_PROWLARR_INDEXERS` no `.env` privado e
acrescente `amigosshare` a `HOMESERVER_RELEASE_INDEXER_PRIORITY`, por exemplo
`uindex,1337x,amigosshare`. Preserve as entradas dos demais indexadores.
O perfil Dublado pesquisa o ASC primeiro e prefere uma candidata dele quando
áudio e qualidade empatam com as outras fontes. Os demais perfis conservam a
ordem configurada. Instalações sem ASC continuam usando as fontes públicas.

Faça login no ASC com “Lembrar-me”. No DevTools do navegador, copie o valor
completo de `Cookie` em **Network → Request Headers** de um pedido ao site.
Cole-o no campo Cookie do Prowlarr, execute **Test** e salve. Ao renovar a
sessão, atualize também a entrada privada do `.env`, que é a configuração
persistente do projeto. O ASC usa sua sessão autenticada diretamente e não
recebe a etiqueta do proxy Byparr. A [definição oficial do Prowlarr](https://raw.githubusercontent.com/Prowlarr/Indexers/master/definitions/v11/amigosshare.yml)
documenta esse acesso; não é necessário adicionar Jackett.

`BYPARR_ENABLED` controla o proxy para desafios Cloudflare. Provedores de
legendas usam `SUBTITLE_PROVIDERS` e `BAZARR_PROVIDERS`, com suas credenciais:

- `SUBDL_API_KEY`.
- `OPENSUBTITLES_USERNAME` e `OPENSUBTITLES_PASSWORD`.
- API keys de Sonarr, Radarr, Prowlarr, Bazarr, Jellyfin e Seerr.
- `ARR_TOKEN`, `ADMIN_TOKEN` e `CSRF_TOKEN` do controlador.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `QBIT_USERNAME` e `QBIT_PASSWORD`.

Todos usam o prefixo `HOMESERVER_` e ficam diretamente no `.env`.
Jellyfin/Seerr podem ter key vazia no primeiro bootstrap. Senhas de contas
existentes não são redefinidas silenciosamente.

## Jellyfin: administrador e coleções

`HOMESERVER_ADMIN_USERNAME` identifica a conta administradora gerenciada.
O apply garante administração e permissões para coleções, legendas e letras,
preservando os demais campos da política e os outros usuários. A preferência
`HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION` aceita `true`, `false` ou vazio;
vazio preserva a permissão de exclusão já configurada.

`HOMESERVER_JELLYFIN_AUTO_COLLECTIONS` é booleano e tem padrão `true`.
Ele controla a criação nativa de coleções na biblioteca de filmes gerenciada,
sem substituir suas demais opções. Filmes com a mesma coleção no TMDb são
agrupados durante a varredura quando há pelo menos dois membros locais.
Configure a frequência de varredura nas tarefas agendadas do Jellyfin.
Definir `false` desativa a criação automática; não apaga coleções existentes.

`HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED=true` habilita o worker a preencher
capas Primary ausentes. `JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS=300` define
o intervalo de reconciliação, em segundos. O ID exato da coleção é obtido dos
filmes associados e a capa é baixada pelo Jellyfin do TMDb. A rotina preserva
capas existentes, nomes, bloqueios e demais metadados. Coleções vazias, com IDs
conflitantes ou sem arte oficial disponível aguardam identificação manual.

As rotinas de apresentação do Jellyfin também são executadas durante a importação
do acervo auditado. Filmes com país de produção Brasil e identidade TMDb confirmada
recebem o título brasileiro, `PreferredMetadataLanguage=pt-BR`, país de metadados
`BR` e pôster português oficial. A seleção prefere a arte da busca brasileira;
na ausência dela, usa outra imagem Primary portuguesa do mesmo filme no TMDb.
O provedor pode identificar essas imagens apenas como `pt`, sem distinguir a região.
Se não houver pôster português disponível, o título e as preferências são aplicados
e somente a capa fica pendente para as próximas tentativas.
O worker verifica novos filmes no mesmo intervalo de reconciliação, mantendo
caminhos, identidades, bloqueios, ordenação personalizada e metadados dos estrangeiros.
A regra para filmes brasileiros permanece ativa mesmo quando as capas de coleções
estão desabilitadas; ela depende da conexão autenticada do Jellyfin.

O painel qBit oferece administração completa por loopback/Tailscale com login
nativo. Alterações manuais são diretas. Preferências gerenciadas pelo `.env`
voltam ao valor declarado quando o operator executa `config apply`; use o
`.env` para mudanças que devem persistir nas próximas reconciliações.

## Monitoramento e invariantes

`METRICS_INTERVAL_SECONDS` define a coleta; `METRICS_MAX_AGE_SECONDS` define
quando dados ficam antigos. `LOG_LEVEL`, `LOG_MAX_SIZE_MB` e `LOG_MAX_FILES`
controlam os logs próprios. Alertas são opcionais por `ALERTS_ENABLED`,
`ALERT_WEBHOOK_URL` e `ALERT_RETRY_SECONDS`.

Completed Download Handling permanece desabilitado nos Arr
(`enableCompletedDownloadHandling=false`) e hardlinks habilitados
(`copyUsingHardlinks=true`). Essas guardas são internas: o worker valida e importa
cada filme, podendo continuar a busca de legenda depois da publicação no Jellyfin.
Séries mantêm a validação de legendas e a ordem de publicação. O apply reutiliza
IDs dos recursos gerenciados existentes, com a migração de perfis obsoletos
descrita acima; verify detecta alterações divergentes feitas nas UIs nativas.

No Radarr, `skipFreeSpaceCheckWhenImporting=true` evita que a importação por
hardlink exija espaço para uma segunda cópia do filme. O gateway já confere o
tamanho real antes do download; na importação, o worker testa o hardlink com o
usuário do Radarr e só permite cópia se houver espaço suficiente. Essa opção
não altera a configuração do Sonarr.

Portas e paths atuais estão em [instalação](installation.md) e
[README](../README.md#acessar). Para mudar uma convenção estrutural, adapte o
Compose/código de forma explícita, mantendo as redes internas e o gateway.

# Planejamento incremental de mídia

Requisito do proprietário: disponibilizar o conteúdo já validado no HD de 18 TB
e acrescentar o restante por lotes, mantendo os discos de origem. A janela de
parada ainda não foi liberada. O plano operacional autorizado continua em
[migração do homelab](../plans/2026-11-14-homelab-refactor-migration.md).

## Problema e recorte

Os helpers históricos privados de consolidação exigem prova global, árvores
inteiras e um corte que inclui limpeza das origens. Não devem ser executados
para realizar o novo fluxo incremental. A manutenção do controlador também não
contém, por si só, scanners nativos de Jellyfin/Arr.

Esta etapa entrega um planejador puro de lotes no consumidor de mídia: recebe
inventário normalizado e referências de evidência produzidas por um futuro
adaptador confiável; calcula grupos completos, conflitos, estado declarado das
provas e demanda lógica de bytes. Não lê disco, copia, publica, altera banco,
claims, manutenção ou configuração. Não produz autorização operacional.

## Contrato

- Uma geração identifica o snapshot, a origem e o destino. Toda evidência deve
  corresponder à geração, ao grupo e ao digest canônico de seu manifesto.
- Cada grupo representa um inode físico e todos os seus paths relativos. Seu
  ID é único no inventário; o adaptador precisa comprovar a completude real dos
  hardlinks. A lista também declara dependências entre grupos (sidecars etc.).
- Selecionar um path inclui o grupo inteiro e o fechamento de dependências.
  Dependência ausente, ID/path inválido, seleção desconhecida, grupo ambíguo ou
  colisão entre grupos impede um plano positivo. Paths não podem escapar da raiz.
- Colisão de path entre grupos bloqueia mesmo quando o hash coincide: igualdade
  de conteúdo não decide sozinha a identidade/posse do objeto nem autoriza merge.
- Evidências são fases distintas: staging verificado, publicação verificada e
  leitura nativa verificada. Publicação sem cadeia de evidências correspondente
  não equivale a disponibilidade. Evidência de outra geração ou manifesto não
  promove estado. O planejador não autentica recibos nem executa os hashes.
- O total de bytes soma cada grupo uma vez, preservando hardlinks. É uma demanda
  lógica conservadora; não representa espaço livre, sparse allocation, xattrs,
  reservas, margem nem capacidade garantida do destino.
- Resultados são determinísticos e não modificam a entrada. Mesmo que todas as
  fases estejam declaradas, `execution_allowed`, `source_cleanup_allowed`,
  `global_scan_allowed` e `deletion_allowed` permanecem falsos nesta etapa.

O estado descritivo do lote nunca substitui fences, locks, quiescência,
contenção nativa de scanners, verificação do USB, prova recente ou leitura real.
Uma alteração de geração invalida a associação anterior, inclusive para retries.

## Aceite e próximo adaptador

Testes sintéticos em Python 3.12/Linux cobrem grupos/dependências completos,
colisões, paths malformados, provas antigas/incompletas, deduplicação física,
determinismo e ausência de I/O. Os guards atuais permanecem intactos. Uma revisão
independente precede publicação em `refactor`.

A próxima etapa deverá produzir o inventário e evidências a partir do pipeline
real, demonstrar contenção de scanners e implementar um executor com journal,
locks e retorno, preservando fontes. Não há aceite do corte nem integração de
runtime nesta entrega. A plataforma mantém autoridade sobre discos/mounts/exports;
o consumidor mantém a semântica dos lotes e do acervo.

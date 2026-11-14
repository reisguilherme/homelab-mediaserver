# Arquitetura do homelab em evolução

Data: 14/11/2026. Estado: **proposta de arquitetura para revisão**, com decisões do proprietário indicadas abaixo. Nenhuma migração ou alteração operacional foi executada nesta análise.

A implementação foi autorizada posteriormente. O [registro de preparação](implementation-status.md)
documenta o acesso SSH, o checkpoint privado e o código preparado; o corte permanece
aguardando aviso do proprietário. Os limites desta análise documental abaixo se
referem à etapa original de desenho.

A recomendação é começar com **Proxmox em um nó e três VMs: entrada, reprodução e núcleo privado**. O núcleo mantém armazenamento e automação juntos para preservar as garantias existentes; Jellyfin recebe apenas leitura da mídia. Kubernetes entra depois, quando houver recursos e cargas que justifiquem vários nós. Um storage dedicado pode ser introduzido antes ou independentemente do Kubernetes.

## Documentos

| Documento | Conteúdo |
|---|---|
| [As-is](as-is.md) | Componentes, estado, redes, layouts, diagrama e evidências do projeto atual |
| [To-be](to-be.md) | Alternativas, desenho inicial, desenho com três nós, storage, segurança de rede e dimensionamento |
| [Operação e governança](operations.md) | Dashboards, controle completo de VMs, acessos, auditoria, alertas e orçamento de observabilidade |
| [Energia e recuperação USB](power-recovery.md) | Nobreak pendente, mecanismo atual, reintegração host/guest/NFS, integridade e critérios de parada |
| [Segurança](security.md) | Ameaças, achados estáticos, controles existentes, riscos residuais e verificações |
| [Gaps e evolução](gaps.md) | Prioridades, dependências, critérios de aceite, migração, retorno e decisões pendentes |
| [Preparação da implementação](implementation-status.md) | Estado observado, checkpoint privado, código preparado e dependências do corte |
| [Diagramas em HTML](diagrams.html) | Diagramas renderizados, navegação e impressão; abre localmente sem rede |

Os diagramas estão em Mermaid nos documentos, junto das explicações, e renderizados no HTML local. Ao alterar os diagramas Mermaid, regenerar o HTML para manter a correspondência. Os links para código apontam para o working tree analisado; as linhas indicadas são referências desta revisão e podem mudar.

## Premissas e decisões

| Item | Estado |
|---|---|
| Primeiro compute | Informado pelo proprietário: i7-10750H, 16 GB DDR4, dois SSDs de 512 GB |
| Mídia | Informado pelo proprietário: um HD USB de 18 TB |
| Demanda | Até dez reproduções simultâneas; upload anunciado de 300 Mbit/s |
| Internet | Proprietário relata IP público, mas ainda não confirmou ausência de CGNAT nem o caminho IPv6 |
| Público | **Confirmado:** reprodução Jellyfin com login individual; administração privada |
| Escrita na mídia | **Confirmado para o to-be:** Jellyfin somente leitura; alterações pela automação privada |
| Administração | **Confirmado:** controle completo, incluindo criação/exclusão de VMs, recursos e acesso por VM; proposta: Proxmox + Grafana + operações HomeServer na área privada |
| Falha de energia | **Relato:** notebook continua na bateria, mas fonte AC do HD desliga; comportamento efetivo ainda não ensaiado |
| Recuperação USB | **Requisito confirmado:** conter a falha e reintegrar automaticamente após validações; situações inseguras permanecem bloqueadas |
| Nobreak | **Confirmado como pendência de aquisição:** proteger fonte do HD e compute, com autonomia e integração a dimensionar |
| Orquestração inicial | Proposta: Compose dentro das VMs; não implantar Kubernetes no primeiro corte |
| Seerr | Proposta: privado inicialmente; publicação futura seria ampliação explícita da superfície pública |
| Estado e continuidade | Proposta: instâncias únicas para o estado atual; sem promessa de alta disponibilidade no primeiro nó |

Os 18 TB nominais correspondem a aproximadamente 16,37 TiB antes de overhead. Isso é conversão de unidade, não medição do disco. Os dois SSDs não significam automaticamente um pool de 1 TB livre, espelhamento ou reserva disponível para uma VM.

## O que muda no desenho

- O processo público deixa de compartilhar o kernel de convidado e os segredos da automação. O hypervisor continua sendo uma dependência comum.
- O storage oferece arquivos por NFS ao playback. Discos de VMs usam um backend de storage do Proxmox; objetos S3 são uma necessidade separada.
- O controlador continua responsável por capacidade, importações, hardlinks e exclusões. Esses contratos são preservados antes de qualquer distribuição entre hosts.
- O isolamento é imposto fora da VM pública, com firewall do hypervisor e, para proteger a LAN mesmo diante de comprometimento do host, equipamento de rede independente.
- Crescer em compute passa a ser possível por etapas; tolerar falha de disco/hub continua exigindo um projeto de armazenamento separado.
- A área privada reúne visão global e controle completo, preservando acesso direto ao Proxmox quando o core ou o portal falham. Telemetria não recebe credenciais de administração.
- Recuperação USB passa a abranger passthrough, core e clientes NFS; nobreak previne perda imediata de energia do HD, enquanto software contém e trata falhas observáveis.

## Limites da conclusão

A análise cobriu código, Compose, configurações versionáveis, guias atuais e documentação oficial dos produtos. A base foi o working tree em torno de `d5dbcd6`, com 312 entradas locais modificadas/não rastreadas no início; não foi auditado apenas o commit. O grafo existente, de setembro, ajudou a localizar temas, mas contém arquitetura histórica e não representa o estado atual.

Não foram consultados `.env`, bancos, tokens, inventários privados ou mídia. Não houve scan das imagens instaladas, pentest, teste físico, acesso ao servidor, verificação de Tailscale/roteador, benchmark ou reprodução em clientes. Achados no código não demonstram exposição WAN ou configuração efetiva do host. As verificações propostas estão em [gaps.md](gaps.md).

As instruções atuais do projeto continuam valendo: sem formatação, alteração de mounts/fstab, ativação de RTX, abertura de portas do roteador ou exclusão automática de mídia. Este desenho não reintroduz CI/CD, releases, gerenciador de backups ou units próprias do host. Cópias verificadas para migração e recuperação são requisitos operacionais; nenhuma nova infraestrutura foi criada.

## Verificação documental em 14/11/2026

Foram conferidos 104 links locais nos sete documentos Markdown, sem destino ausente ou referência de linha fora do arquivo. Os sete diagramas Mermaid passaram por parsing e renderização no navegador; o HTML contém os sete SVGs e não depende de recursos externos. Fences e whitespace foram conferidos, e a visualização de operação/recuperação foi inspecionada.

A revisão independente identificou a necessidade de autorização destrutiva independente do Jellyfin; o contrato foi incorporado ao to-be e ao gap G19, e reavaliado pelo revisor. Essa validação é do desenho. A suíte do aplicativo e os ensaios de servidor permanecem fora desta entrega documental.

A revisão da ampliação de operação/energia corrigiu três pontos: liberação do estado NFS antes de remontar, distinção entre pausas anteriores e novas intenções durante recuperação, e observação da queda do core fora do Grafana hospedado nele. Os três ajustes foram reavaliados pelo revisor. Nenhuma ativação de recuperação, integração Proxmox ou proteção elétrica foi executada ou demonstrada no servidor.

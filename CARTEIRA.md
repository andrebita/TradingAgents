# Carteira de dividendos das grandes da B3

Uma rotina que mantém uma carteira de 10 ações por regra. A inteligência artificial lê e alerta, mas não escolhe
ações. Toda a evidência por trás dela está em `experiments/br_baseline/README.md`.

> **Não é recomendação de investimento.** O resultado passado não garante o futuro, e a vantagem histórica não
> passou no teste estatístico rigoroso que fixamos antes de rodar.

> **Veículo padrão recomendado para a parte em ações: o DIVO11.** O replay da rotina real (aportes mensais, ações
> inteiras, isenção de R$ 20 mil, IR, seleção congelada) rendeu 12,0% ao ano de TIR, contra 11,1% do DIVO11 com
> os mesmos aportes, de 2012 a 2026. A diferença de 0,9 ponto ficou abaixo do mínimo de 1 ponto fixado antes de
> rodar, e a queda foi maior (−59% contra −52%). A rotina fica como opção para quem quer a regra própria.
>
> Um ponto a favor das ações, fora do critério: venda de ações até R$ 20 mil por mês é isenta, e venda de ETF não
> é. Detalhes em `experiments/br_baseline/README.md`, "Replay da rotina real".

## Carteira por índices (a política recomendada desde a revisão 5)

Os testes de 2009 a 2026 levaram a uma carteira simples:
- **Seleção de ações:** seis regras de seleção (dividendos, qualidade, valor, momentum, baixa volatilidade e a
  combinação) ficaram atrás dos ETFs de índice depois de custos e IR.
- **Alocação:** a mistura com ações globais teve a maior chance de ganhar do Tesouro Selic em 5 anos (78% no
  bootstrap, ou 60% sem a desvalorização passada do real), contra cerca de 50% só com ações brasileiras.

Detalhes em `experiments/br_baseline/README.md`, seções "Revisão 5".

Para usar, a política ganha um bloco `index_plan`:

```json
"index_plan": {"brazil": 0.20, "international": 0.40, "fixed_income": 0.40,
               "brazil_menu": ["BOVA11", "DIVO11"], "brazil_default": "BOVA11",
               "international_vehicle": "WRLD11", "horizon_end": "2031-11-01",
               "migration_start": "2026-11-01", "listed_funds_exit_months": 12}
```

**O que o código faz a cada reunião:**
1. **Reserva primeiro:** só conta caixa e pós-fixado diário.
2. **Migração do que está fora do plano:**
   - ações individuais saem dentro da isenção de R$ 20 mil por mês, das maiores para as menores;
   - fundos listados saem aos poucos, ao longo dos meses definidos. Um fundo sem preço médio fica bloqueado até
     você informar o preço, porque sem ele o IR não pode ser calculado; o ritmo passa para o fundo seguinte;
   - Tesouro IPCA+ e prefixados que vencem depois do seu horizonte vão para o Tesouro Selic. Sem uma data de
     saque (`horizon_end`), vale uma janela móvel de `bond_max_years` anos (5 por padrão): o preço desses títulos
     oscila muito com os juros, e a régua do plano é a Selic;
   - o resto da renda fixa fica até o vencimento.
3. **Dinheiro da venda de ações individuais:** compra primeiro o ETF Brasil. É uma troca dentro da mesma classe:
   a migração não tira dinheiro do Brasil para o exterior depois de uma queda, nem o contrário.
4. **Dinheiro novo e o resto da migração:** vai para a classe mais abaixo do alvo, em cotas inteiras de ETF e em
   Tesouro Selic. O título é o de maior taxa entre os que vencem até o fim do seu horizonte, com a taxa do dia do
   Tesouro Direto; se nenhum vence até lá, o primeiro que vence depois.
5. **Transição:** quando faltam ações além da faixa, sai da Selic acima da reserva a diferença dividida pelos
   meses que faltam da transição. Assim ela termina no prazo.
6. **Rebalanceamento:** só em junho e dezembro, e só quando uma classe passa do alvo mais a faixa. O dinheiro da
   venda, líquido do IR, vai para a classe mais abaixo do alvo. Sem preço médio, a venda fica bloqueada.
7. **Situação de mercado:** a queda de cada ETF desde a máxima de 12 meses. A partir de 15%, a carta traz o plano
   de crise escrito; a partir de 30%, chama uma reunião extraordinária. Enquanto o episódio está aberto, a queda
   é medida desde o pico do episódio, mesmo depois de ele sair da janela de 12 meses.

**O diretor:**
- escolhe o ETF da parte Brasil dentro do cardápio (BOVA11, DIVO11 ou metade de cada). A escolha vale para o
  dinheiro novo, porque vender ETF paga 15% de IR. Com um só ETF no cardápio, não há escolha: trocar de índice ou
  incluir uma inclinação a dividendos é mudança de política, decidida por você;
- pode adiar até duas vendas de migração. O ritmo se mantém: antes de decidir, ele vê o que o código vende no
  lugar de cada venda adiada;
- dá os pontos que a carta deve passar e o que só você pode decidir.

Numa queda, ele não sugere vender por causa do preço. Se a sua situação mudou (precisa do dinheiro numa data,
ou não aguenta mais o risco), ele pede uma revisão da política.

Valores, ritmo e alvos são do código, e a checagem recusa o que sair do cardápio.

**Os analistas** leem o plano do mês antes do diretor:
- **renda fixa:** confere a reserva, os resgates da regra do horizonte, os vencimentos e os emissores;
- **risco:** a queda num estresse simultâneo, em % e em R$ do patrimônio investível;
- **tributário:** o IR das vendas e resgates;
- **macro:** juros, inflação e câmbio, só como contexto.

Eles leem a política sem os campos da regra antiga de ações. Quando o diretor adia uma venda, a carta mostra o
que mudou no plano depois da decisão. As pendências que o diretor repete das do código saem da lista.

**A carta** abre com a carteira recomendada ativo a ativo: o que comprar, vender, resgatar, aplicar ou manter,
com quantidade, preço de referência, valor e motivo. Depois vêm a ordem de execução (vender antes de comprar, com
as datas de liquidação), o IR (o que não pode ser calculado aparece como tal, nunca como zero) e as pendências.

O texto da carta é escrito depois da decisão do diretor, sobre o plano final. O código confere cada valor em R$
que o texto cita contra os dados e avisa quando um não aparece neles.

## Comitê de investimentos

Para o patrimônio inteiro, e não só as ações, use o comitê:
- **Cinco analistas:** renda fixa, ações, risco, tributário e macro.
- **Um diretor financeiro**, que decide dentro da sua política e escreve uma carta mensal.
- **Uma checagem em código:** cada decisão é conferida contra a política e o patrimônio, e trazida de volta aos
  limites quando sai deles.

**O que a checagem garante:**
- **Reserva de emergência:** conta só caixa e pós-fixados (Selic, CDI) com liquidez diária. Um Tesouro IPCA+
  ou prefixado sai em um dia, mas a preço de mercado, então não é reserva. O aporte completa a reserva primeiro,
  e nada vai da renda fixa para ações enquanto ela estiver incompleta.
- **Resgates:**
  - só de posições que existem, pelo nome exato, e até o saldo;
  - nunca dentro da reserva;
  - nunca antes do vencimento num título sem liquidez diária;
  - um resgate antecipado a preço de mercado vai para você confirmar.
- **Aplicações:**
  - só com o dinheiro liberado no mês: a parte do aporte que fica na renda fixa e os resgates;
  - só em títulos públicos, ou em CDB, LCI e LCA com o banco informado, dentro do limite do FGC.
- **Ações:**
  - a transferência sai de fontes escolhidas pelo código: caixa primeiro, depois pós-fixados acima da reserva,
    os tributados antes dos isentos;
  - o dinheiro do mês nunca leva as ações acima do alvo mais a faixa.
- **Patrimônio incompleto:** uma posição sem cotação recente e sem valor informado, ou um ETF sem classe, não
  vale zero. Ela bloqueia a transferência e aparece na carta.
- **Falhas:** se o diretor falhar (recusa, resposta inválida ou orçamento), vale a decisão da regra, e a carta
  avisa.
- **Orçamento:** cada chamada reserva o pior custo possível antes de sair. Os alertas da rotina têm um terço do
  orçamento, para sobrar espaço para o diretor.
- **Textos externos:** fatos relevantes e notícias entram como dados, nunca como instruções. O poder do diretor
  é limitado pela checagem de qualquer forma.

**A carta:**
1. Abre com o plano já conferido: datas dos dados; o patrimônio hoje e depois do plano; cada movimentação de
   dinheiro, de onde para onde; as ordens de ações; o IR estimado; o que só você pode confirmar; e caixas para
   aprovar.
2. Depois vem a carta do diretor, escrita antes da checagem. Onde divergir, vale o plano.
3. Por fim, os relatórios dos analistas.

```bash
tradingagents comite reunir --politica ~/.tradingagents/minha/politica.json \
    --patrimonio ~/.tradingagents/minha/patrimonio.json --aporte 3000
```

- **Modelos de arquivo:** `examples/comite/politica_exemplo.json` e `examples/comite/patrimonio_exemplo.json`;
  para a carteira por índices, `examples/comite/politica_indices_exemplo.json`. Todos os valores são fictícios.
  - **No patrimônio:**
    - um ETF diz em que classe conta: `"asset_class": "acoes"`, `"internacional"` ou `"renda_fixa"`;
    - BDRs contam como internacional;
    - o bloco `tax` traz o que você já vendeu de ações no mês, o ganho dessas vendas e o prejuízo acumulado.
  - **Na política:**
    - `max_drawdown_tolerance` é a maior queda do patrimônio investível que você aceita (0.20 = 20%). Sem
      ela, o comitê pede o número em vez de supor um. O analista de risco mostra a queda de cada mistura
      num estresse simultâneo: ações −59%, fundos −30%, renda fixa estável.
    - `fixed_income_kinds` limita onde o dinheiro novo de renda fixa pode ir; por exemplo, `["tesouro_selic"]`
      para tudo em Tesouro Selic.
- **Modelo de cada membro:** fica na política. O padrão é Fable 5.1 para o diretor, Sonnet 5.5 para os analistas
  de renda fixa, risco e tributário, e Haiku 5.5 para os alertas e o macro.
- **Custo medido:** US$ 0,63 por reunião, com teto configurável (`budget_usd`).
- **Saídas:** a carta vai para `~/.tradingagents/logs/comite/carta_AAAA-MM-DD.md`, junto com o registro das
  decisões (`decisoes.jsonl`) e a carteira-sombra do diretor (`sombra_diretor.json`). A sombra mede, daqui para
  frente, se o julgamento dele agrega à regra.

**Seus dados não vão para o repositório.** Guarde a política, o patrimônio e os extratos fora da pasta do projeto,
por exemplo em `~/.tradingagents/minha/`. O `.gitignore` também bloqueia `patrimonio*.json`, `politica*.json`,
`minha_carteira*.json`, extratos em PDF, cartas, relatórios e registros de decisão, caso algum vá parar ali por
engano.

## A regra

- **Universo:** as 50 empresas mais negociadas da B3.
- **Seleção:** entre as que pagaram proventos em cada um dos últimos 3 anos, as **10 com maior dividendo médio dos
  últimos 24 meses** sobre o preço, com pesos iguais.
- **Rebalanceamento:** em **junho e dezembro**. Nos outros meses só entra dinheiro novo, nas ações da seleção, e
  nada é vendido. Assim uma carteira que começa do zero é montada aos poucos, com os aportes.

**Histórico (abr/2012 a out/2026, com proventos reinvestidos, sem IR):**

| | Retorno anual | Maior queda |
|---|---|---|
| Regra, rebalanceamento trimestral | 11,2% | −59% |
| Regra, rebalanceamento semestral (o adotado aqui) | 13,5% | −62% |
| DIVO11 | 9,8% | −56% |
| Ibovespa | 7,5% | −47% |

**Com os atritos da rotina real** (replay de mar/2012 a out/2026, R$ 100 mil mais R$ 3 mil por mês):

| | TIR | Retorno anual da cota | Maior queda |
|---|---|---|---|
| Rotina, semestral | 12,0% | 10,2% | −59% |
| Rotina, trimestral | 12,1% | 10,4% | −59% |
| DIVO11, mesmos aportes | 11,1% | 9,9% | −52% |
| BOVA11, mesmos aportes | 8,9% | 7,6% | −39% |
| CDI líquido | 8,4% | 8,3% | 0% |

- **Atenção:** a semestral foi escolhida depois de ver as duas, então seu número carrega risco de seleção. A
  trimestral, pré-registrada como principal, é a referência mais honesta.
- **A favor:** ganhou do Ibovespa em 83% das janelas de 3 anos.
- **Contra:** perdeu em 5 de 15 anos, caiu mais nas crises e depende bastante de 2016.

## Renda fixa x ações

Quanto do patrimônio fica em ações é decisão sua; a rotina mantém a proporção escolhida. Decidir *quando* trocar
(por tendência ou por valuation) perdeu de manter uma proporção fixa nos testes.

Histórico de ago/2012 a jul/2026, com rebalanceamento em junho e dezembro e CDI líquido de IR (15%, aproximado):

| Ações | Com a carteira de dividendos | Pior queda | Pior ano | Com o Ibovespa no lugar |
|---|---|---|---|---|
| 0% | 8,2% ao ano | 0% | +2% | 8,2% ao ano |
| 20% | 9,7% | −7% | +1% | 8,6% |
| 30% | 10,4% | −11% | −1% | 8,7% |
| 40% | 11,0% | −17% | −5% | 8,8% |
| 50% | 11,5% | −25% | −9% | 8,9% |
| 60% | 12,0% | −31% | −13% | 8,9% |
| 80% | 12,8% | −43% | −21% | 8,8% |
| 100% | 13,3% | −56% | −29% | 8,6% |

**Como ler a tabela:**
- A coluna "com o Ibovespa" é o cenário em que a regra de dividendos deixa de ter vantagem: aí quase não compensa
  sair do CDI.
- **Como funciona na rotina:**
  - com `--renda-fixa` e `--alvo-acoes`, o aporte vai primeiro para a classe abaixo do alvo;
  - uma diferença maior que a faixa (5 pontos) vira uma transferência mensal da renda fixa para as ações, ao longo
    de `--meses-transicao` meses;
  - ações acima do alvo são corrigidas mandando os aportes para a renda fixa, sem vender.

## O que a rotina faz

1. **Seleção-alvo:**
   - recalcula as 10 ações da regra em junho e dezembro, com preços e proventos do Yahoo e cadastro da CVM;
   - nos outros meses, mantém a última seleção registrada, como no teste;
   - os setores que você excluir ficam fora;
   - uma ação com salto de preço sem explicação fica fora de compras novas por um ano. Saltos que se desfazem em
     até 10 pregões são tratados como erro do Yahoo e zerados.
2. **Ordens sugeridas, com IR estimado:**
   - vende o que saiu da seleção, mantendo as vendas do mês em até **R$ 20 mil** (isenção de IR para pessoa
     física);
   - o resto fica em `vendas_pendentes.json` e é retomado todo mês, dentro da isenção;
   - várias corretoras com a mesma ação contam como uma posição, a preço médio ponderado;
   - o IR estimado fica reservado no caixa, e o restante do caixa, do aporte e das vendas compra as ações mais
     abaixo do peso;
   - não vende ganhadoras que continuam na seleção;
   - cuida só de ações de empresas listadas. FIIs, ETFs e BDRs aparecem como fora da rotina e nunca são
     vendidos por ela.
3. **Alertas por LLM:**
   - toda semana, resume os fatos relevantes, comunicados e notícias de cada posição, com nível normal, atenção ou
     alerta;
   - no rebalanceamento, estima o **risco de corte de dividendo** de cada ação a partir dos demonstrativos
     anonimizados. No teste, o modelo acertou bem (AUC 0,69), mas excluir as ações marcadas reduziu o retorno,
     porque o mercado já precifica o corte. Por isso é só alerta.
4. **Carteira-sombra:** registra cada seleção e mede o resultado daqui para frente contra BOVA11 e DIVO11.

O relatório sai em Markdown, em `~/.tradingagents/logs/carteira/relatorio_AAAA-MM-DD.md`, ou na pasta indicada
por `TRADINGAGENTS_RESULTS_DIR`.

## Como rodar

```bash
pip install .
export ANTHROPIC_API_KEY=...     # para os alertas; sem chave, use --sem-llm

tradingagents carteira revisar --carteira minha_carteira.json --aporte 1000
```

`minha_carteira.json`, guardado fora do repositório (o preço médio serve para estimar o IR):

```json
{
  "cash": 500.0,
  "currency": "BRL",
  "positions": [
    {"ticker": "TAEE11", "quantity": 300, "average_price": 32.10},
    {"ticker": "BBAS3", "quantity": 400, "average_price": 22.00}
  ]
}
```

| Opção | Efeito |
|---|---|
| `--aporte 1000` | Dinheiro novo desta revisão |
| `--rebalancear` / `--sem-rebalancear` | Força ou impede o rebalanceamento (padrão: junho e dezembro) |
| `--sem-llm` | Sem alertas, custo zero |
| `--orcamento 1.0` | Teto de gasto com LLM por execução, em US$ |
| `--vendas-no-mes`, `--ganho-no-mes` | O que você já vendeu neste mês, para a conta da isenção |
| `--prejuizo-acumulado` | Prejuízo de meses anteriores a compensar |
| `--data AAAA-MM-DD` | Revisão numa data passada |
| `--renda-fixa 100000` | Seu valor atual em renda fixa |
| `--alvo-acoes 30` | Percentual do patrimônio em ações |
| `--meses-transicao 12`, `--faixa 5` | Ritmo da transição e tolerância, em pontos percentuais |

**Custo medido:** cerca de US$ 0,08 numa revisão com rebalanceamento (3 alertas e 10 riscos de corte), e menos de
US$ 0,03 numa revisão semanal. A primeira execução baixa os preços de ~400 ações (cerca de 1 minuto); as seguintes
usam o cache por 12 horas.

## Rodar toda semana

Três opções:
- **cron no seu computador**, por exemplo toda segunda às 19h:
  `0 19 * * 1 cd ~/TradingAgents && tradingagents carteira revisar --carteira ~/minha_carteira.json`
- **uma tarefa agendada do Claude Code na nuvem**, que roda o comando e salva o relatório no repositório;
- **GitHub Actions** com agendamento semanal e a chave como secret.

Em todas, a carteira-sombra (`carteira/carteira_sombra.json`) precisa persistir entre as execuções.

## Limitações

- **Preços e proventos do Yahoo:**
  - saltos que se desfazem são tratados como erro e zerados;
  - saltos que ficam são mantidos e marcados para conferência;
  - o Yahoo não tem várias empresas que saíram da bolsa, então o histórico tem viés de sobrevivência.
- **IR estimado:** apenas para operações comuns de ações. Day trade, outros ativos e mudanças na lei ficam de fora;
  confirme com um contador.
- **Tributação de dividendos a partir de 2026:** não modelada.
- **Ordens:** são sugestões; a execução é sua.

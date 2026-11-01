# Handoff: comitê de investimentos (branch `ccr-521e8d8e-6a4fal`)

Atualizado em 01/11/2026. Este arquivo é público, como o repositório: **não tem dados pessoais**. O contexto privado
(valores, posições, ids do Drive e do Supabase, resultados) está no documento "HANDOFF privado" da pasta do Google
Drive **"TradingAgents - dados privados do comitê"**, do próprio investidor.

## 1. O objetivo, nas palavras do investidor

> "O objetivo é criar um comitê de analistas e diretor financeiro que gere carteira, recomenda ativos,
> balanceamento, análise técnica, conjuntura, etc, mais próximo possível de um comitê real de agentes humanos de
> gestão."

Patrimônio pessoal, no Brasil. O comitê **recomenda**, e o investidor executa. A meta declarada é uma "gestão
pró-ativa especializada de carteira de investimentos no Brasil".

## 2. Onde estamos, e por que o investidor está frustrado

A última mensagem do investidor (01/11/2026): o que foi construído "não está convergindo" para o objetivo acima.
Ele tem razão, e o próximo agente precisa partir daí.

**O que desviou.** A cada revisão, uma decisão saiu dos agentes e foi para o código:

1. O concurso de seleção (`experiments/br_baseline`, revisões 4 e 5) mostrou que nenhuma regra de escolha de ações
   batia os ETFs de índice depois de custos e IR.
2. Então a carteira virou um **plano de índices**: 20% BOVA11, 40% WRLD11 e 40% Tesouro Selic, com migração
   mecânica de tudo o que estiver fora disso (`tradingagents/committee/indices.py`).
3. Nas revisões 5 e 6, o diretor (Fable 5.1) ficou só com o **percentual em ações, entre 40% e 60%**, e mais
   pouca coisa (`director.py`, `check_index`).

O resultado é uma calculadora com uma carta. A primeira reunião paga recomendou vender por regra posições que o
investidor queria ver avaliadas uma a uma. Ele tinha dito: "se as empresas forem sólidas, com boas perspectivas,
não me importo em manter... confiarei no diretor financeiro". **Nenhuma ordem foi executada.** As cartas de
01/11/2026 **não devem ser seguidas** antes do redesenho.

**O backtest reforçou o diagnóstico.** Foi feito em 01/11 com os extratos da B3, e os resultados estão só no Drive
privado. Um comitê de verdade teria olhado papel por papel, não vendido tudo por regra.

## 3. A proposta de redesenho (aguarda as decisões do investidor)

Um comitê no formato de um comitê humano. O código vira **compliance**: limites, impostos, reserva e orçamento.
Ele **não decide**.

1. **Política (investidor):** objetivo, reserva, faixas por classe, proibições. Já existe em `policy.py`; ganha as
   faixas.
2. **Reunião mensal com pauta:**
   - **Conjuntura:** Selic, IPCA, câmbio, Focus e fiscal → cenário → o que muda na alocação. Usar
     `dataflows/vendors/bcb.py`, que já respeita as datas de divulgação.
   - **Alocação tática:** o diretor define o peso de cada classe dentro das faixas (pós-fixado, IPCA+, prefixado,
     ações Brasil, FIIs, exterior).
   - **Ações:** analista fundamentalista (CVM: DFP/ITR) e analista técnico (indicadores de preço) por papel.
     Recomendam manter, comprar, reduzir ou vender, com tese, preço-alvo e gatilhos de revisão, e podem sugerir
     papéis novos de um universo definido.
   - **FIIs:** analista próprio (P/VP, dividend yield, vacância, crédito).
   - **Renda fixa:** escolhe títulos e prazos (por exemplo, IPCA+ longo quando o juro real está alto) em vez de
     vender por regra.
   - **Risco:** contraponto obrigatório a cada proposta (advogado do diabo), concentração e stress.
   - **Tributário:** o custo de cada movimento (já existe a lógica de isenção, regressivo e DARF).
   - **Diretor:** decide e escreve a ata, com o porquê e o que monitorar.
3. **Acompanhamento:** cada tese vira um item com gatilho (tabela `pending_items` no Supabase). Um gatilho
   disparado convoca reunião extraordinária.
4. **Prestação de contas:** a carteira do comitê é comparada todo mês com o **plano de índices, que deixa de ser a
   carteira e vira o benchmark**. Cada recomendação fica registrada com o resultado. É o teste realista do valor
   dos agentes: backtest com LLM no passado tem vazamento, porque o modelo já sabe o que aconteceu.

**Reaproveitar a base original do TradingAgents**, que já tem o que falta:
- `tradingagents/agents/analysts/` (`market_analyst` = técnico, `fundamentals_analyst`, `news_analyst`,
  `sentiment_analyst`);
- `researchers/` (debate otimista × pessimista);
- `risk_mgmt/` (debate agressivo/neutro/conservador);
- `managers/` (`research_manager`, `portfolio_manager`);
- `graph/trading_graph.py`.

O experimento 0 (`experiments/br_baseline/README.md`) mostrou que, para a B3 no passado, só os preços chegam aos
agentes. Os conectores CVM/BCB do fork existem para isso.

**Decisões pendentes do investidor (perguntar antes de construir):**
1. **Faixas por classe.** Proposta: ações Brasil 10–35%, exterior 0–30%, FIIs 0–15%, IPCA+/prefixado 0–25%, o
   resto em pós-fixado, reserva fixa.
2. **Universo de ações:** só as que ele tem, ou elas mais recomendações novas (por exemplo, o IBrX-100)?
3. **Orçamento por reunião.** O redesenho deve custar algo como US$ 2–4. É uma estimativa: medir numa reunião de
   teste. O teto atual é `budget_usd` = 3,0.

**Primeira entrega sugerida:** uma reunião em que os analistas revisam cada posição atual e recomendam manter ou
sair, com tese. É o que mais falta hoje.

## 4. Mapa do código (`tradingagents/committee/`)

| Módulo | O que faz |
|---|---|
| `policy.py` | Política (IPS), `IndexPlan`, modelos por papel (`Models`), `for_models()` |
| `wealth.py` | Patrimônio: renda fixa, listados (com `lent` para aluguel), estado tributário |
| `indices.py` | Plano mensal do modo índices: reserva, migração, metas, ordens, rebalanceamento, crise. O redesenho deve tirar daqui as decisões e deixar os cálculos (IR, isenção, regressivo) |
| `analysts.py` | Analistas (renda fixa, ações, risco, tributário, macro), `Models.llm`, limites de saída (analistas 15k, carta 15k, diretor 40k tokens) |
| `director.py` | Decisão do diretor, conferência pela política (`check_index`), carta (Sonnet) e auditoria dos R$ da carta (`audit_letter`) |
| `index_meeting.py` / `meeting.py` | Reunião completa, render da carta, log `decisoes.jsonl`, sincronização com o Supabase |
| `store.py` | Supabase via REST: políticas, patrimônio, negociações, preços médios, reuniões, ordens, pendências |
| `b3_import.py` | Extrato de negociação da B3 → preço médio |
| `b3_history.py` | Extratos da B3 → carteira real mês a mês, fluxos de dinheiro e IR estimado |
| `backtest.py` | Simula o plano de índices com o mesmo dinheiro (`simulate`), métricas (`summary`) |

Também:
- CLI: `tradingagents comite reunir --politica P --patrimonio W` ou `--do-banco` (lê do Supabase); `--sem-llm`
  roda só os fatos.
- Backtest: `experiments/backtest_comite/run.py`; uso no docstring.
- Banco: `supabase/migrations/`.
- Exemplos fictícios: `examples/comite/`.
- Evidência dos testes de seleção e alocação: `experiments/br_baseline/README.md` e `CARTEIRA.md`.

## 5. Regras do investidor (valem para qualquer agente)

- **Nenhum dado pessoal no git** (o repositório é público). Valores, posições e extratos ficam só no ambiente
  privado da sessão, no Drive e no Supabase. Testes usam dados fictícios. O fixture `_no_personal_store` em
  `tests/conftest.py` impede que um teste toque o Supabase do investidor.
- **Nunca pedir chave de API no chat.** Nunca imprimir `TRADINGAGENTS_SUPABASE_KEY`. As chaves vêm do ambiente:
  `TRADINGAGENTS_CLAUDE_KEY` ou `ANTHROPIC_API_KEY`, e `TRADINGAGENTS_SUPABASE_KEY`.
  `TRADINGAGENTS_SUPABASE_URL` ainda não foi configurada pelo investidor.
- O investidor **autorizou formalmente** todas as requisições ao projeto Supabase dele (id no handoff privado).
- **Perguntar antes de qualquer execução paga.** O saldo de API é pequeno (cerca de US$ 10 na última conta, a
  confirmar). Uma reunião do modo índices custou cerca de US$ 0,56 (Fable ~0,45, Sonnet ~0,10).
- Não criar nem publicar fork, contratar serviços ou gerar despesas sem autorização.
- Pesquisa delegada: preferência por um agente Sonnet com esforço médio.
- Conversar em português. O código e os comentários estão em inglês.

## 6. Ambiente e dados

- Testes: `python -m pytest -q` (1.587 passando em 01/11/2026). Lint: `ruff check`.
- **Dados que não sobrevivem ao container:**
  - os arquivos COTAHIST e o `spot.csv.gz`. Para baixar de novo, usar a B3 (SerHist) e rodar
    `experiments/br_baseline/cotahist/parse.py`;
  - os extratos da B3 do investidor (negociação, movimentação, posição). Pedir que ele envie de novo;
  - os caches de Tesouro, CDI e Yahoo, que os vendors refazem sozinhos.
- O `eventos.json` do backtest (conversões de código que os extratos não explicam) está no Drive privado.

## 7. Pendências que já existiam

**Do investidor:**
- enviar as ofertas de CDB, LCI e LCA;
- confirmar a liquidez diária da LIG;
- configurar `TRADINGAGENTS_SUPABASE_URL`.

**Sem custo, a oferecer de novo depois do redesenho:**
- *earnings yield* das ações com dados da CVM;
- diretor com esforço médio;
- `budget_usd` 4,0.

**No Supabase:** as pendências abertas em `pending_items` (preços médios de bonificações, enquadramento de FI-Infra,
liquidez, limites por CPF etc.).

**Uma limitação do backtest:** `b3_history.listed_taxes` cobra 20% de todo fundo listado, mas FI-Infra da Lei
12.431 é isento para pessoa física. Não afetou o resultado, porque não houve venda com ganho.

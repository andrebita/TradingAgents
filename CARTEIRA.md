# Carteira de dividendos das grandes da B3

Uma rotina que mantém uma carteira de 10 ações por regra. A inteligência artificial lê e alerta, mas não escolhe
ações. Toda a evidência por trás dela está em `experiments/br_baseline/README.md`.

> **Não é recomendação de investimento.** O resultado passado não garante o futuro, e a vantagem histórica não
> passou no teste estatístico rigoroso que fixamos antes de rodar.

## A regra

- **Universo:** as 50 empresas mais negociadas da B3.
- **Seleção:** entre as que pagaram proventos em cada um dos últimos 3 anos, as **10 com maior dividendo médio dos
  últimos 24 meses** sobre o preço, com pesos iguais.
- **Rebalanceamento:** em **junho e dezembro**. Nos outros meses só entra dinheiro novo, e nada é vendido.

**Histórico (abr/2012 a out/2026, com proventos reinvestidos, sem IR):**

| | Retorno anual | Maior queda |
|---|---|---|
| Regra, rebalanceamento trimestral | 11,2% | −59% |
| Regra, rebalanceamento semestral (o adotado aqui) | 13,5% | −62% |
| DIVO11 | 9,8% | −56% |
| Ibovespa | 7,5% | −47% |

- **A favor:** ganhou do Ibovespa em 83% das janelas de 3 anos.
- **Contra:** perdeu em 5 de 15 anos, caiu mais nas crises e depende bastante de 2016.

## O que a rotina faz

1. **Seleção-alvo:** recalcula as 10 ações da regra, com preços e proventos do Yahoo e cadastro da CVM.
2. **Ordens sugeridas, com IR estimado:**
   - vende o que saiu da seleção, mantendo as vendas do mês em até **R$ 20 mil** (isenção de IR para pessoa
     física) e adiando o resto;
   - usa caixa, aporte e vendas para comprar as ações mais abaixo do peso;
   - não vende ganhadoras que continuam na seleção.
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

`minha_carteira.json` (o preço médio serve para estimar o IR):

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

- **Preços e proventos do Yahoo:** erros de ajuste de eventos corporativos são neutralizados por regra, mas o Yahoo
  não tem várias empresas que saíram da bolsa, então o histórico tem viés de sobrevivência.
- **IR estimado:** apenas para operações comuns de ações. Day trade, outros ativos e mudanças na lei ficam de fora;
  confirme com um contador.
- **Tributação de dividendos a partir de 2026:** não modelada.
- **Ordens:** são sugestões; a execução é sua.

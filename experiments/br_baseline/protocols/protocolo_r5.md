# Revisão 5 — protocolo proposto para a disputa de ações

Versão: 31/10/2026. Estado: pronto para registro, ainda não registrado nem executado. Nenhum arquivo do repositório foi alterado. Referência auditada: andrebita/TradingAgents, commit 2a919b9. O histórico até 30/10/2026 já é explorado; este documento não o transforma em amostra intocada. A checagem exploratória do NEFIN 2001–2011 feita nesta revisão também fica declarada como conhecida.

## 1. Pergunta e candidatas congeladas

Pergunta: alguma implementação simples de seleção de ações brasileiras oferece vantagem líquida relevante sobre os veículos passivos disponíveis, suficiente para compensar concentração, trabalho e risco de erro?

Oito braços, sem alavancagem ou posições vendidas:

| ID | Seleção |
|---|---|
| DY | Regra atual: top 50 por liquidez; proventos positivos em cada uma das três janelas anteriores de 12 meses; dez maiores DY24; pesos de 10%; sem limite setorial. DY24 = proventos por ação dos 24 meses anteriores / 2 / preço atual. |
| Q | Dez maiores notas de qualidade, definidas abaixo; peso-alvo de 10%; máximo de duas empresas por setor FCA. |
| V | Dez maiores lucros por ação/preço, definidos abaixo; mesmos pesos e limite. |
| M | Dez maiores retornos de momentum, definidos abaixo; mesmos pesos e limite. |
| L | Dez menores volatilidades realizadas, definidas abaixo; mesmos pesos e limite. |
| QVM | Dez maiores médias dos percentis Q, V e M, com um terço para cada componente; mesmos pesos e limite. |
| BOVA | Comprar e manter BOVA11, reinvestindo fluxos no próprio veículo. Referência principal. |
| DIVO | Comprar e manter DIVO11. Referência adicional; sua inclusão não pressupõe superioridade. |

As cinco regras fatoriais e BOVA são as seis alternativas novas à regra DY. DIVO é um controle já existente. Exterior será avaliado na alocação, separadamente da seleção Brasil.

## 2. Universo, dados e sinais

Em cada decisão, partir de todas as empresas que estavam listadas naquela data, incluindo as posteriormente retiradas de negociação. Uma empresa por CNPJ; escolher a classe de maior mediana de volume financeiro nos 63 pregões anteriores. Exigir pelo menos 42 observações na janela e preço com defasagem de no máximo dez dias corridos. Ordenar as empresas por essa mediana e tomar as 50 maiores antes dos filtros específicos de cada sinal. Empates: CNPJ crescente; para classe, ticker crescente.

Usar volume em dinheiro e preços efetivamente negociados, reconstruindo desdobramentos, grupamentos, bonificações, cisões, conversões e cancelamentos. Não multiplicar preço retroativamente ajustado por volume incompatível. Não confundir ausência de cotação com perda de 100%, nem excluir uma falência porque seu histórico saiu do Yahoo. Eventos sem solução documental bloqueiam a conclusão confirmatória sobre o trecho afetado; não se apaga o retorno extremo por conveniência.

Cadastro, setor e balanços precisam estar públicos antes da execução. Corte informacional: fim do dia da decisão; execução apenas no pregão seguinte. Toda versão documental deve ter data de entrega anterior ou igual ao corte. Se só houver uma reapresentação futura, não usar seus números antigos retroativamente. A ausência causada por reapresentação também impede chamar a reconstrução de perfeitamente point-in-time. Guardar documentos e suas versões prospectivamente.

Classificação setorial: valor do campo Setor_Atividade do FCA válido naquela data, normalizado por caixa, espaços e acentos, sem reagrupamentos discricionários depois dos resultados. Setor ausente: inelegível nas cinco regras com limite setorial. O limite de 20% é sobre os pesos-alvo; a flutuação de preços pode ultrapassá-lo entre rebalanceamentos.

Percentil p(x): (posto médio crescente - 1)/(n - 1); se n=1, usar 0,5. Calcular entre as empresas com dados válidos para aquele componente, dentro das 50. Nenhuma otimização de pesos, janelas ou número de posições.

Qualidade Q:

- Usar três exercícios anuais consecutivos, encerrados e publicados; o último encerramento não pode ter mais de 550 dias. Buscar quatro saldos anuais de patrimônio para os denominadores.
- ROE de cada exercício = lucro líquido consolidado do exercício / média do patrimônio líquido consolidado no início e fim do exercício. Lucro e patrimônio devem incluir o mesmo perímetro de participação de não controladores; não misturar individual e consolidado.
- Exigir patrimônio positivo em todos os saldos usados e ativo total positivo. Não preencher contas ou exercícios faltantes com zero.
- a = média aritmética dos três ROEs; b = desvio-padrão amostral dos três ROEs; c = patrimônio líquido/ativo total do último exercício.
- Q = [p(a) + p(-b) + p_setor(c)]/3. O terceiro percentil é calculado apenas dentro do setor FCA; com menos de três empresas válidas no setor, fixá-lo em 0,5. Assim, não se compara diretamente a capitalização contábil de um banco com a de uma indústria.
- Essa é uma proxy simples de rentabilidade, estabilidade e capitalização; não equivale a ROIC nem a índice regulatório de solvência.

Valor V:

- V = lucro básico anual por ação da classe negociada / preço da ação na decisão.
- Usar a DFP anual mais recente disponível, com encerramento há no máximo 550 dias, e LPA positivo. Não misturar LPA ON, PN ou de units. Units sem reconciliação documental de direitos econômicos ficam inelegíveis.
- Ajustar o LPA somente por eventos de quantidade ocorridos entre a referência do documento e a decisão, mantendo o preço e o LPA na mesma base. Não usar número de ações atual para fabricar lucro por ação histórico.
- É uma regra de earnings yield anual, não de lucro normalizado ou TTM; lucros extraordinários e o ciclo econômico são riscos deliberadamente não corrigidos após o teste.

Momentum M:

- M = TR(t-21)/TR(t-252) - 1, com índices em pregões; TR é a série de retorno total reconstruída com eventos conhecidos até a decisão.
- Exigir 253 preços válidos e eventos conciliados. Não usar o preço bruto, que penalizaria distribuições de proventos.
- Rebalancear semestralmente como os demais braços. Isso testa uma implementação de baixo giro; não replica exatamente o fator acadêmico de atualização mensal.

Baixa volatilidade L:

- L = menos o desvio-padrão amostral dos últimos 252 retornos totais diários, multiplicado por raiz de 252.
- Exigir a mesma história e conciliação de M. Não alavancar a carteira para igualar beta.

Multifator QVM:

- Exigir Q, V e M válidos. Recalcular os percentis dos três sinais sobre a interseção elegível e tirar a média de pesos iguais.
- Não adicionar baixa volatilidade, dividendos, valuation macro ou opiniões do comitê.

Seleção das cinco regras fatoriais: percorrer a ordem decrescente de nota, aceitar no máximo duas empresas de cada setor e parar em dez. Se faltarem elegíveis, manter cada posição escolhida com alvo de 10% e o restante em caixa, sem redistribuir o peso. Publicar cobertura e caixa por data.

## 3. Motor e execução comuns

Desacoplar o seletor do motor existente, preservando a contabilidade comum. Todos os braços devem atravessar o mesmo processamento de ordens, eventos, impostos e cotização; a função separada de benchmark com frações fictícias não será usada para decidir.

- Primeira seleção na data inicial. Depois, atualizar seletores no último pregão de junho e dezembro. Congelar a lista entre essas datas; BOVA e DIVO permanecem no mesmo veículo.
- Planejar no fechamento da decisão e executar no fechamento do pregão seguinte, com preço de execução distinto do preço de planejamento. Aportes mensais ficam disponíveis para esse pregão.
- Ações e cotas inteiras; lote fracionário para ações, unidade de cota para ETF. Primeira montagem pode ter dez ordens. Rotina posterior: no máximo três compras e três vendas mensais por braço, com vendas antes das compras.
- Vender primeiro posições fora da seleção, em ordem de maior excesso em reais; depois, excessos sobre o peso-alvo. Comprar os três maiores déficits em reais, limitando ao alvo e ao caixa disponível. Desempate por CNPJ/ticker. Carregar ordens de venda pendentes; revalidá-las contra o alvo mais recente.
- Para ações, planejar vendas até R$ 19 mil por mês e revalidar o limite legal de R$ 20 mil com preços efetivos e todas as vendas de ações do contribuinte. A regra não pode conceder uma isenção por ticker, corretora ou subcarteira. O simulador de seleção isolada não assume outras vendas; a simulação da carteira completa agrega tudo.
- Se uma venda não puder ser executada dentro do limite, adiar; registrar exposição residual e atraso. Não escolher retrospectivamente os meses de realização de lucro.
- Custo principal: 0,15% do valor por ordem, compra e venda, para todos os braços. Sensibilidade: 0,30%. Taxas internas de ETF já presentes na cota não serão descontadas novamente.
- Caixa residual na corretora não rende, para todos os braços. A parcela estratégica de Tesouro Selic será modelada separadamente, com rendimentos, marcação a mercado, taxas e tributação por lote.
- Ações: cotação sem ajuste de dividendos para a contabilidade, quantidade corrigida por eventos, direito a proventos registrado na data ex e caixa apenas na data de pagamento. Recebíveis entram no patrimônio para evitar uma perda fictícia entre data ex e pagamento. Não somar dividendos ao preço já ajustado por dividendos.
- ETF: série de preços investível, com tratamento de eventual distribuição efetiva; despesas e retenções internas já incorporadas ao fundo. Não dar ao cotista reinvestimento gratuito ou cotas fracionárias inexistentes.
- Regime principal: regras tributárias de 2026 constantes, como comparação econômica para quem começa hoje; replay com regras históricas é sensibilidade separada. JCP: 17,5%; dividendos: aplicar o limite mensal por empresa informado no problema; ações: isenção mensal e 15% quando tributável; ETF de ações: 15%, sem isenção. Apurar perdas compensáveis nas categorias legalmente compatíveis, custos no preço médio e prazo efetivo de pagamento/reserva do imposto.
- Se dividendos e JCP não puderem ser separados documentalmente, publicar limites de resultado supondo 0% e 100% dos proventos ambíguos como JCP. Não aprovar uma vencedora cuja vantagem desapareça nesse intervalo. Isso é análise de sensibilidade, não substituto definitivo do dado.
- Valor terminal: publicar tanto patrimônio marcado a mercado quanto patrimônio após venda integral, custos e impostos efetivamente incidentes. O primário usa o segundo. Publicar também o IR diferido. Não atribuir liquidação imediata isenta a uma carteira de ações inteira acima de R$ 20 mil.
- Sensibilidade de saída gradual: nos últimos 12 meses da janela, suspender compras de risco, direcionar novos aportes à Selic e vender ações até o limite mensal; usar o mesmo calendário de redução para ETFs. Liquidar e tributar qualquer resíduo no último dia. Não apresentar essa sensibilidade como uma retirada instantânea.

## 4. Períodos e fluxos

Retrospectivo comum: primeira decisão em 30/06/2014; primeira execução no pregão seguinte; término em 30/10/2026. Dados anteriores servem somente para aquecimento dos sinais. Se a reconstrução de alguma família falhar, publicar a falha; não deslocar o início procurando melhor resultado. O retrospectivo 2012–2014 dos braços com dados suficientes é apêndice, sem direito de escolher vencedor.

Teste primário de seleção Brasil: R$ 50.000 iniciais e R$ 1.200 mensais a partir do segundo mês, equivalentes à parcela de 40% de um patrimônio de R$ 125.000 e aporte de R$ 3.000. As demais classes não entram no ranking deste módulo.

Sensibilidades obrigatórias: patrimônio de R$ 100 mil, R$ 125 mil e R$ 150 mil, cruzado com parcela Brasil de 20%, 40% e 60%. Capital inicial e aporte são sempre a fração correspondente do patrimônio e dos R$ 3 mil. Nove combinações incluindo a principal; nenhuma pode ser escolhida retrospectivamente como a que “representa melhor” o investidor.

Subperíodos: jul/2014–dez/2018; jan/2019–dez/2022; jan/2023–out/2026. Anos parciais separados de anos civis completos. Janelas móveis adicionais: 60 meses, com passos mensais e mesmos fluxos. A sobreposição não cria observações independentes.

Prospectivo: primeiro pregão após o registro efetivo até o último pregão de outubro de 2031, se registrado antes do primeiro pregão de novembro de 2026. Se houver atraso, usar 60 meses completos a partir da primeira execução e registrar a nova data antes dela. Publicar acompanhamento anual descritivo, sem trocar regras ou promover vencedor por consultas intermediárias. Corrigir erro demonstrado com nova versão e divulgação do impacto; não reescrever silenciosamente o registro.

## 5. Métricas

Publicar para todos os braços, inclusive os reprovados:

1. TIR/XIRR nominal anual dos fluxos efetivos, primária após liquidação terminal; também antes da liquidação e em reais de poder aquisitivo constante.
2. Retorno total e CAGR da cota, com cotização em cada fluxo e custos/impostos reconhecidos. Publicar série diária, inclusive primeira execução e último dia, sem apagar o custo inicial ou o mês final.
3. Queda máxima diária da cota, datas do pico/fundo e recuperação; resultado mensal apenas como informação secundária.
4. Pior ano civil completo; primeiro e último anos incompletos identificados à parte.
5. IR pago por categoria, IR terminal/diferido, custos de negociação e caixa ocioso.
6. Giro anual de uma via = vendas brutas do ano / patrimônio médio diário; também compras, vendas totais, ordens/mês, pendências e peso efetivo fora do alvo.
7. Exposição a setores, emissores, fatores e participação do melhor ano no resultado relativo.

Não atribuir ao seletor a vantagem criada apenas pelo calendário de aportes; TIR e cota precisam concordar com a decisão econômica.

## 6. Critério congelado de promoção

Há 13 contrastes: os sete braços diferentes de BOVA contra BOVA; os seis seletores de ações (DY, Q, V, M, L, QVM) também contra DIVO. Família fechada; nenhuma outra variante entra sem novo registro.

Inferência: diferenças mensais de log-retorno da cota líquida, bootstrap circular pareado de blocos de 12 meses, 10.000 replicações, semente 521. Mesmos blocos de calendário para todos os braços, preservando a dependência entre estratégias. Teste unilateral de média relativa menor ou igual a zero, bootstrap centrado sob a hipótese nula; corrigir os 13 valores-p por Holm, erro familiar de 5%. Reportar intervalos de 95% e identificá-los como marginais quando não forem simultâneos. Blocos de seis e 24 meses são sensibilidades obrigatórias, sem escolha posterior do melhor.

Uma regra ativa só se qualifica historicamente se cumprir TODOS:

- vantagem de pelo menos 1 ponto percentual ao ano em TIR após liquidação e CAGR da cota, contra BOVA e DIVO;
- valores-p ajustados de seus dois contrastes abaixo de 5%;
- vantagem de CAGR positiva nos dois benchmarks em pelo menos dois dos três subperíodos e em pelo menos 60% das janelas de 60 meses;
- vantagem positiva após retirar 2016 e, separadamente, o ano civil completo de maior contribuição ao excesso de log-retorno contra cada benchmark; isso é diagnóstico sobre retornos mensais existentes, não uma história fictícia com o ano apagado da contabilidade;
- queda máxima em magnitude não excede a de nenhum dos dois benchmarks em mais de cinco pontos percentuais;
- vantagem terminal positiva nas nove escalas, com custo dobrado e nos limites relevantes de JCP; não se exige significância em cada sensibilidade;
- execução respeita o limite de ordens e cobertura de dados permite uma conclusão auditável.

DIVO só substitui BOVA como padrão se passar os requisitos equivalentes contra BOVA. Uma estratégia defensiva pode ser útil mesmo sem passar; não será proclamada melhor motor de crescimento sob outro critério depois de vistos os resultados.

O retrospectivo qualifica para acompanhamento em sombra, não prova uma vantagem independente. A promoção após outubro de 2031 exige novamente vantagem líquida mínima de um ponto, robustez operacional e teste familiar no período prospectivo; não juntar o histórico explorado para fabricar significância. O poder de cinco anos pode ser insuficiente: ausência de aprovação significa manter o padrão, não provar que o fator não existe.

Se mais de uma passar, preferir a de menor número médio de ordens; depois menor custo realizado; depois menor giro. Não criar um otimizador de combinações de vencedoras. Se nenhuma passar, BOVA permanece o veículo Brasil padrão e a carteira global continua segundo a política estratégica.

## 7. NEFIN: validação externa e atribuição

Usar arquivos mensais oficiais, registrando versão, hash, frequência e ponderação. HML é valor patrimonial/preço, não earnings yield; WML é winners-minus-losers, não uma carteira comprada semestralmente de dez ações. Os fatores não são ETFs líquidos de custos, impostos ou aluguel.

Período 2001–2011: contexto temporal anterior à amostra própria, já examinado exploratoriamente nesta revisão. Comparar valor e momentum nas versões ponderada por valor de mercado e por pesos iguais; não escolher a ponderação vencedora. Não afirmar que Q ou L foram validados pelos cinco fatores disponíveis.

Na interseção temporal com cada estratégia, estimar r_carteira - Rf = alfa + beta_mercado*(Rm-Rf) + beta_SMB*SMB + beta_HML*HML + beta_WML*WML + beta_IML*IML + erro, usando o Rf do mesmo arquivo. Reportar erros HAC com seis defasagens, coeficientes, intervalos e R² ajustado. Repetir para DIVO e para o excesso DY-DIVO, e decompor contribuição setorial separadamente. Isso testa se o desempenho coincide com exposições conhecidas; não estabelece causalidade nem identifica qualidade por resíduo.

Os dados de terceiros reduzem dependência da implementação própria, mas 2012–2026 continua sendo a mesma história de mercado. Não tratar proveniência independente como independência estatística, nem o período pré-2012 como confirmação de parâmetros escolhidos agora.

## 8. Alocação e fundos: módulo separado, sem reotimização do seletor

Patrimônio primário R$ 125 mil e aporte mensal R$ 3 mil; extremos de R$ 100 mil/R$ 150 mil como sensibilidades. Reserva de emergência excluída. Comparar políticas fixas: (A) 40% BOVA/60% Selic; (B) 60% BOVA/40% Selic; (C) 20% BOVA/40% ações globais/40% Selic. Um cenário 80% ações é diagnóstico da relação retorno-risco, não aumento automático da tolerância do investidor.

Ações globais: WRLD11 efetivo desde sua existência; IVVB11 é alternativa de exposição aos EUA, não substituto indistinguível de mundo. A comparação investível completa usa o início comum dos veículos. Uma extensão anterior com VT, câmbio, retenção estrangeira e despesas é reconstrução hipotética claramente separada; não colar retornos brutos do S&P 500 ao histórico de WRLD11.

Aportes para maiores déficits em reais. Revisão semestral; só vender para rebalancear quando uma classe estiver mais de cinco pontos percentuais do alvo após o aporte. Na simulação completa, uma única contabilidade fiscal por contribuinte e categoria. Medir TIR real, patrimônio líquido, pior janela de cinco anos, queda máxima e tempo de recuperação. Acrescentar estresses de queda simultânea das bolsas, apreciação do real e juros persistentemente altos. Não estimar pesos de máxima média-variância com este histórico curto.

Fundos listados: testar 0%, 5% e 15%, primeiro substituindo proporcionalmente a parcela de ações da política C e mantendo 40% Selic. Para 15%, isso dá 15% BOVA/30% global/15% fundos/40% Selic. Mostrar separadamente a alternativa financiada pela Selic, porque ela aumenta o orçamento de risco. Comparar XFIX11 com uma regra própria somente em período e cobertura comuns, incluindo fundos encerrados. Para XFIX11, alíquota principal de 15% conforme fato relevante de 27/11/2025; 20% como sensibilidade documental. Não aplicar a tributação do XFIX11 aos FIIs individuais.

Exemplo de regra FII independente de dividend yield: oito FIIs de imóveis físicos, com mais de 24 meses de histórico, selecionados por mediana de volume financeiro de 63 pregões; pesos iguais e no máximo dois por segmento principal informado no documento público; revisão semestral; segmento ausente ou carteira de imóveis físicos inferior a 80% dos ativos: inelegível. Se menos de oito passarem, manter vagas em Selic. É uma hipótese de exposição simples a imóveis, não promessa de alfa. FIagro e FI-Infra exigem módulos próprios de crédito, garantias, duration e tributação; não formar uma cesta única apenas por serem cotas listadas.

## 9. Custo e governança

Todos os módulos quantitativos, extrações determinísticas, regressões, bootstrap e relatórios: US$ 0 de API de LLM; podem consumir tempo de engenharia e infraestrutura. Não chamar comitê no histórico. Comitê sugerido: duas reuniões semestrais e até duas por eventos materiais, a US$ 0,85 cada: US$ 3,40. Limite total de API de US$ 13; preservar US$ 9,60 sem obrigação de gastar. O LLM explica documentos e diferenças, cita fontes e alerta inconsistências; não inventa dados, muda parâmetros ou dispara ordens.

Antes de executar: congelar este texto, código, dependências, manifestos de dados e hashes em registro datado. Documentar limitações de cobertura. Este arquivo propõe a especificação; não afirma que os novos resultados já existem.

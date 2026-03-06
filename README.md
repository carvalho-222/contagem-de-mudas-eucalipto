# Contagem de Mudas de Eucalipto em Mosaicos

Este repositório contém um script Python que **detecta mudas de
eucalipto** em **mosaicos (GeoTIFF)**, gera uma **máscara raster** com
as detecções e exporta um **GeoJSON georreferenciado** com as geometrias
detectadas, além de um **JSON de estatísticas** contendo:

-   quantidade de plantas
-   área analisada (ha)
-   densidade de plantas por hectare
-   métrica de homogeneidade do plantio

O objetivo segue o desafio de P&D da **Bem Agro**: receber um **TIF RGB
georreferenciado** e retornar:

-   **GeoJSON com as detecções**
-   **JSON com estatísticas agronômicas**

com robustez a **sombras e variações cromáticas**.

------------------------------------------------------------------------

# Pipeline do Algoritmo

Para cada mosaico `.tif` ou `.tiff` em `data/raw/`:

## 1. Conversão para escala de cinza

Garante que o mosaico possua **apenas uma banda** - Escala de cinza.

-   Converte imagens **RGB/RGBA**.

Função:

    ensure_grayscale()

------------------------------------------------------------------------

## 2. Garantia de projeção métrica

Se o mosaico não estiver no **EPSG desejado**, o script reprojeta o
raster.

Isso é necessário porque:

-   cálculos de área dependem de **metros**
-   GSD precisa estar em **unidade métrica**

Função:

    ensure_epsg()

------------------------------------------------------------------------

## 3. Padronização do GSD

Todos os mosaicos são reamostrados para um **GSD alvo**.

Exemplo:

    5 cm/pixel

Isso garante consistência nos filtros e cálculos de área.

Operação utilizada:

    gdal.Warp

------------------------------------------------------------------------

## 4. Geração da máscara da área útil

O script gera uma máscara binária da área válida do mosaico, separando
o conteúdo real da imagem do fundo (regiões vazias). Usada para calcular
á área de plantio, descontando as regiões vazias do mosaico.

Etapas:

1.  Leitura do raster em tiles
2.  Threshold de intensidade para separar fundo
3.  Geração de máscara binária georreferenciada

## 5. Cálculo da área útil

A área analisada é calculada a partir da máscara gerada anteriormente.

Etapas:

1.  fechamento morfológico da máscara
2.  remoção de componentes muito pequenos
3.  contagem de pixels válidos
4.  conversão para m² e hectares

------------------------------------------------------------------------

## 6. Processamento em tiles

Para evitar estouro de memória ao processar mosaicos grandes, o raster
é dividido em **blocos (tiles)**.

    tile_size = 512 px

Cada tile passa por um pipeline de visão computacional clássica para
realçar objetos escuros (mudas) e remover ruídos.

Pipeline aplicado em cada tile:

- `bilateralFilter` → redução de ruído preservando bordas
- blur de fundo (`GaussianBlur`) + subtração → realce de objetos escuros
- `GaussianBlur` adicional para suavização
- `Otsu threshold` → segmentação automática
- `MORPH_OPEN` → remoção de pequenos ruídos

### Filtros heurísticos adicionais

Após a segmentação inicial, são aplicados filtros para remover falsos
positivos próximos às bordas do mosaico:

- geração de **máscara da área útil do mosaico**
- detecção da **transição entre área válida e fundo**
- remoção de contornos que:
  - tocam a borda do mosaico **e**
  - possuem forma muito alongada ou baixa circularidade

Isso evita detecções deformadas em regiões externas ao mosaico.

### Refinamento morfológico

A máscara segmentada passa por operações adicionais:

- erosão
- dilatação

Essas operações ajudam a consolidar objetos e remover ruídos, dando
ênfase na copa das árvores.

### Filtro de área física

Os contornos detectados são filtrados com base em sua área real,
convertendo **pixels → m²** usando o GSD do raster.

Intervalo atual:

    0.25 m² – 4.0 m²

Apenas objetos dentro desse intervalo são mantidos.

O resultado final dessa etapa é uma **máscara raster binária
georreferenciada**, onde:

| valor | significado |
|------|-------------|
| 0 | fundo |
| 255 | objeto segmentado |

Arquivo gerado:

    data/processed/mask_<nome>.tif

------------------------------------------------------------------------

## 7. Conversão da máscara para polígonos

A máscara raster binária gerada na etapa anterior é convertida para
**polígonos georreferenciados**.

Etapas:

1. extração de contornos com `OpenCV`
2. conversão de coordenadas **pixel → coordenadas geográficas**
3. criação de polígonos vetoriais com **Shapely**

Cada polígono resultante representa uma **muda detectada**.

Função utilizada:

    mask_to_polygons_full()

Esses polígonos são posteriormente exportados para **GeoJSON**.
Ao fim de cada execução os arquivos intermediários localizados em processed
são removidos.
------------------------------------------------------------------------

# Estrutura de Pastas

O projeto espera a seguinte organização:

    data/
     ├─ raw/
     │   └─ mosaicos .tif/.tiff
     |
     └─ processed/
         └─ arquivos intermediários

    outputs/
     ├─ geojson
     └─ stats.json

    notebooks/
     ├─ calcular_area_util_ha.ipynb
     └─ testes_morfologicos.ipynb

Entrada obrigatória:

    data/raw/*.tif

Observação: O diretório notebooks/ contém 2 arquivos .ipynb que foram desenvolvidos apenas para testes a fim de encontrar as melhores abordagens e parâmetros para desenvolvimento do algoritmo código principal (app/main.py).

------------------------------------------------------------------------

# Principais dependências

Recomendado:

-   Python 3.11+
-   GDAL
-   OpenCV
-   Shapely
-   GeoPandas
-   Pyproj
-   Fiona

------------------------------------------------------------------------

# Instalação

Arquivo requirements.txt contém todas as dependências necessárias para
rodar o script. Para instalá-las, rode no terminal:

pip install -r requirements.txt

------------------------------------------------------------------------

# Como Executar

## 1. Coloque os mosaicos

    data/raw/
       sample1.tif
       sample2.tif

------------------------------------------------------------------------

## 2. Configuração

Arquivo:

    app/main.py

Parâmetros principais:

``` python
config = {
  "target_epsg_code": 31982,
  "target_gsd_cm": 5,
  "gsd_tolerance": 5e-4,
  "block_size": 512,
  "warp_memory_gb": 25,
}
```

### target_epsg_code

EPSG utilizado para processamento.

Exemplo:

    31982 (UTM)

------------------------------------------------------------------------

### target_gsd_cm

Resolução espacial alvo.

Exemplo:

    5 cm/pixel

------------------------------------------------------------------------

### gsd_tolerance

Tolerância para verificar se o mosaico já está com GSD correto.

------------------------------------------------------------------------

### block_size

Tamanho do tile carregado na memória.

Controla consumo de RAM.

------------------------------------------------------------------------

### warp_memory_gb

[IMPORTANTE] Quantidade máxima de RAM que o **GDAL Warp** pode utilizar.
Quanto maior esse valor mais rápido é o processamento. No entanto, deve
ser usado com cautela para não estourar o limite de memória da sua máquina.

------------------------------------------------------------------------

# Uso de Memória no GDAL Warp

Regra prática:

usar **30%--40% da RAM da máquina**.

Ex:  Máquina com 16 GB de RAM -> Usar 4GB - 6GB

Para ambientes compartilhados:

usar **20--30% da RAM**.

------------------------------------------------------------------------

# Execução

Após configurar:

    python app/main.py

O script irá:

1.  Encontrar `.tif` em `data/raw`
2.  Processar cada mosaico
3.  Gerar arquivos de saída

------------------------------------------------------------------------

# Saídas Geradas

## Arquivos intermediários (temporários)

Durante a execução, os seguintes arquivos podem ser criados em:

    data/processed/


### GeoTIFF em escala de cinza

data/processed/<nome>_gray.tif

Contém o mosaico original convertido para **uma única banda em escala
de cinza**, preservando:

- geotransform
- sistema de coordenadas
- dimensões do raster

---

### GeoTIFF padronizado

data/processed/<nome>_pad.tif

Contém o mosaico após:

- reprojeção para o **EPSG configurado**
- padronização do **GSD**
- tratamento de fundo inválido como `NoData = 0`

Este raster é utilizado como base para todas as etapas seguintes do
pipeline.

---

### Máscara da área útil

data/processed/area_util_<nome>.tif

Máscara binária utilizada para calcular a **área útil analisada** do
mosaico.

Valores:

| Valor | Significado |
|------:|-------------|
| 0 | fundo / área inválida |
| 255 | área útil do mosaico |

Essa máscara é utilizada para estimar a área total válida em hectares.

---

### Máscara de segmentação

data/processed/mask_<nome>.tif

Máscara binária contendo os objetos detectados pelo algoritmo.

Valores:

| Valor | Significado |
|------:|-------------|
| 0 | fundo |
| 255 | objeto segmentado |

A máscara mantém:

- CRS original
- resolução espacial
- geotransform

Esses objetos serão posteriormente convertidos em polígonos.

---

**Observação:**  
Os arquivos dentro de `data/processed/` são **temporários** e são
removidos automaticamente ao final do processamento de cada mosaico
para evitar acúmulo de dados intermediários.

---

## Arquivos finais

### GeoJSON de detecções

outputs/<nome>.geojson

Arquivo vetorial contendo as geometrias detectadas.

Características:

- CRS de saída: **EPSG:4326**
- cada polígono representa **uma muda detectada**
- contém também o atributo:

| campo |
|------|
| `area_m2` |

representando a área estimada da muda.

---

### Estatísticas

outputs/<nome>_stats.json

Arquivo contendo métricas agronômicas calculadas a partir das detecções.

Inclui:

- nome do mosaico
- área útil analisada (hectares)
- total de plantas detectadas
- densidade de plantas por hectare
- métricas de homogeneidade do plantio

Exemplo:

```json
{
  "mosaico": "sample1",
  "area_ha": 12.345,
  "total_plantas": 1234,
  "plantas_por_ha": 99.94,
  "homogeneidade": {
    "media_tamanho_m2": 0.1234,
    "desvio_padrao_m2": 0.0123,
    "coeficiente_variacao_percentual": 9.98,
    "quartil_inferior_m2": 0.1150,
    "quartil_superior_m2": 0.1310,
    "homogeneidade_classificacao": "Alta"
  }
}```

------------------------------------------------------------------------

# Validação no QGIS

1.  Abrir o mosaico `.tif`
2.  Abrir o `GeoJSON`
3.  Sobrepor camadas

Verificar:

-   projeção adequada
-   contagem aproximada
-   falsos positivos
-   mudas não detectadas

------------------------------------------------------------------------

# Limitações Conhecidas

### Sensibilidade à iluminação

Sombras fortes ou variações abruptas de iluminação podem afetar a
segmentação, uma vez que o algoritmo depende de contraste entre as
mudas e o solo.

------------------------------------------------------------------------

### Dependência do GSD

Os filtros de área assumem que as mudas possuem tamanho aproximado entre:

    0.25 m² – 4.0 m²

Caso o mosaico possua resolução muito diferente do GSD alvo ou mudas
em estágio muito diferente de crescimento, o filtro pode remover
objetos válidos ou manter falsos positivos.

------------------------------------------------------------------------

### Segmentação baseada em intensidade

A separação entre mudas e solo é baseada principalmente em contraste
de intensidade após subtração de fundo. Em cenários onde:

- o solo possui tonalidade semelhante à copa
- há presença de resíduos vegetais
- o contraste espectral é baixo

o algoritmo pode apresentar falsos positivos ou falhas de detecção.

------------------------------------------------------------------------

### Processamento em tiles

O mosaico é processado em blocos independentes para reduzir consumo
de memória. Isso pode ocasionar efeitos de borda caso um objeto esteja
dividido entre dois tiles.

------------------------------------------------------------------------

### Filtros heurísticos

Filtros baseados em:

- circularidade
- comprimento
- área mínima/máxima

podem remover casos extremos legítimos, especialmente mudas com
morfologia atípica ou parcialmente ocluídas.

------------------------------------------------------------------------

# Roadmap Futuro

Possíveis melhorias incluem:

-   overlap entre tiles para reduzir artefatos de borda
-   paralelização do processamento por tiles
-   calibração automática de parâmetros baseada em GSD
-   métricas de qualidade (precision/recall)
-   deduplicação de objetos próximos
-   integração com modelos de **Deep Learning**
-   uso de informações espectrais adicionais

------------------------------------------------------------------------

# Observação Final

Este pipeline foi projetado com os seguintes objetivos:

-   **não depender de treinamento de modelos**
-   utilizar **visão computacional clássica**
-   manter **baixa complexidade computacional**
-   permitir execução rápida em **mosaicos geoespaciais de grande porte**.
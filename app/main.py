from osgeo import gdal, osr
import sys
import numpy as np
import cv2
import os
from shapely.geometry import Polygon
import geopandas as gpd
import json
import shutil

gdal.UseExceptions()

# --- CONFIGURAÇÕES ---
config = {
    "target_epsg_code": 31982,
    "target_gsd_cm": 5,
    "gsd_tolerance": 5e-4,
    "block_size": 512,
    "warp_memory_gb": 25,
    "paths": {
        "input": 'data/raw/',
        "temp": 'data/processed/',
        "output": 'outputs/'
    }
}

def ensure_grayscale(src_ds, output_path, block_size=1024):
    """
    Converte um raster RGB/RGBA para grayscale em blocos, sem carregar tudo em RAM.
    Salva o resultado em GeoTIFF.
    """
    print("\n[ETAPA] Iniciando conversão para escala de cinza...")

    if src_ds.RasterCount < 3:
        raise RuntimeError("[FALHA] Dataset não possui bandas RGB suficientes.")

    xsize = src_ds.RasterXSize
    ysize = src_ds.RasterYSize
    gt = src_ds.GetGeoTransform()
    proj = src_ds.GetProjection()

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        output_path,
        xsize,
        ysize,
        1,
        gdal.GDT_Byte,
        options=[
            "TILED=YES",
            "COMPRESS=DEFLATE",
            "BIGTIFF=IF_SAFER"
        ]
    )
    if out_ds is None:
        raise RuntimeError(f"[FALHA] Falha ao criar arquivo grayscale: {output_path}")

    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(proj)

    out_band = out_ds.GetRasterBand(1)
    out_band.SetNoDataValue(0)

    r_band = src_ds.GetRasterBand(1)
    g_band = src_ds.GetRasterBand(2)
    b_band = src_ds.GetRasterBand(3)

    for y0 in range(0, ysize, block_size):
        y_block = min(block_size, ysize - y0)

        for x0 in range(0, xsize, block_size):
            x_block = min(block_size, xsize - x0)

            r = r_band.ReadAsArray(x0, y0, x_block, y_block).astype(np.float32)
            g = g_band.ReadAsArray(x0, y0, x_block, y_block).astype(np.float32)
            b = b_band.ReadAsArray(x0, y0, x_block, y_block).astype(np.float32)

            gray = 0.299 * r + 0.587 * g + 0.114 * b
            gray = np.clip(gray, 0, 255).astype(np.uint8)

            out_band.WriteArray(gray, x0, y0)

    print("[SUCESSO] Ortomosaico convertido para escala de cinza.")
    out_band.FlushCache()
    out_band = None
    out_ds = None

def get_crs_from_ds(ds):
    """
    Extrai o código (EPSG) do sistema de referência de coordenadas (CRS).
    Lê os metadados do mosaico para identificar o sistema de projeção atual,
    essencial para validar se os dados estão em um padrão métrico (UTM).

    Args:
        ds (gdal.Dataset): Dataset georreferenciado.

    Returns:
        str or None: Código EPSG (ex: '31982') ou None se não georreferenciado.
    """

    wkt = ds.GetProjection()
    if not wkt: return None

    crs = osr.SpatialReference()
    crs.ImportFromWkt(wkt)
    return crs.GetAuthorityCode(None)

def ensure_epsg(ds, target_code):
    """
    Garante que o mosaico esteja projetado no sistema de coordenadas desejado.
    Caso o CRS original seja diferente do target, realiza uma reprojeção virtual 
    (Warp) para assegurar que os cálculos de área e distância sejam coerentes.

    Args:
        ds (gdal.Dataset): Dataset em escala de cinza.
        target_code (int): Código EPSG de destino (ex: 31982).

    Returns:
        gdal.Dataset: Dataset georreferenciado no sistema correto.
    """

    print("\n[ETAPA] Verificando Sistema de Coordenadas...")
    current_code = get_crs_from_ds(ds)
    if current_code != str(target_code):
        target_crs = f"EPSG:{target_code}"
        print(f"\n[ETAPA] Padronizando sistema para EPSG:{target_code}...")
        return gdal.Warp('', ds, format='VRT', dstSRS=target_crs)
    
    print(f"[SUCESSO] Mosaico já está no sistema desejado.")
    return ds

def _build_creation_opts():
    """
    Define as opções de criação de arquivos GeoTIFF para o driver GDAL.
    Configura o particionamento interno em blocos (Tiling) para otimizar a leitura, 
    habilita o uso de múltiplos núcleos da CPU para acelerar a escrita e define o suporte 
    a arquivos de grande porte (BigTIFF). A compressão é mantida como 'NONE' para preservar 
    a integridade absoluta dos valores originais de reflectância dos pixels.

    Returns:
        list: Lista de strings com parâmetros de criação do GDAL.
    """

    opts = [
        "TILED=YES",
        f"BLOCKXSIZE={config['block_size']}",
        f"BLOCKYSIZE={config['block_size']}",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
        "COPY_SRC_OVERVIEWS=YES",
        "COMPRESS=NONE"
    ]
    return opts

def _get_warp_memory_bytes(memory_available):
    """
    Calcula o limite de memória cache para operações de Warp.
    Define o teto de RAM que o GDAL pode utilizar para realizar o reprocessamento 
    geográfico (reprojeção e redimensionamento). Um valor alto permite o processamento 
    de mosaicos densos com menos acessos ao disco, aumentando a eficiência do método.

    Args:
        memoria_disponivel (int): Quantidade de memória em Gigabytes (GB).

    Returns:
        int: Valor convertido para Bytes.
    """

    return memory_available * 1024 * 1024 * 1024

def progress_cb(complete, message, unknown):
    """
    Função de callback para monitoramento visual do progresso de tarefas do GDAL.
    Provê feedback em tempo real no console sobre o percentual de conclusão de 
    operações custosas como o Warp, atualizando a interface a cada incremento de 10%.

    Args:
        complete (float): Valor entre 0.0 e 1.0 indicando o progresso.
        message (str): Mensagem de status enviada pelo GDAL.
        unknown: Parâmetro de dados de usuário (não utilizado).

    Returns:
        int: 1 para continuar o processamento ou 0 para cancelar.
    """

    percent = int(complete * 100)
    if percent % 10 == 0:
        sys.stdout.write(f"\r   Processando: {percent}%")
        sys.stdout.flush()
    return 1

def padronizar_gsd(ds_utm, creation_opts, warp_mem_bytes, output_path):
    """
    Padroniza a resolução espacial (GSD) do mosaico e preserva o tratamento
    de pixels inválidos durante o reprocesamento.

    Verifica se a resolução atual do raster difere do GSD alvo definido nas
    configurações. Caso necessário, executa um Warp com interpolação bilinear
    para reamostrar o mosaico, mantendo o valor de NoData da origem e
    padronizando o fundo inválido como 0 no arquivo de saída. Se o GSD já
    estiver adequado, apenas realiza uma cópia para o arquivo final.

    Args:
        ds_utm (gdal.Dataset): Dataset georreferenciado no sistema métrico alvo.
        creation_opts (list): Opções de criação do arquivo GeoTIFF de saída.
        warp_mem_bytes (int): Limite de memória em bytes para a operação de Warp.
        output_path (str): Caminho onde o raster padronizado será salvo.

    Returns:
        None: O raster padronizado é salvo diretamente em disco.
    """
    print("\n[ETAPA] Avaliando a Necessidade de Padronização do GSD...")

    gt = ds_utm.GetGeoTransform()
    rx, ry = float(gt[1]), float(abs(gt[5]))
    target_res_m = config['target_gsd_cm'] / 100.0

    band = ds_utm.GetRasterBand(1)
    src_nodata = band.GetNoDataValue()
    if src_nodata is None:
        src_nodata = 0

    need_warp = abs(rx - target_res_m) > config['gsd_tolerance']

    if need_warp:
        print("Iniciando Warp (Bilinear)...")
        warp_opts = gdal.WarpOptions(
            format="GTiff",
            xRes=target_res_m,
            yRes=target_res_m,
            resampleAlg=gdal.GRA_Bilinear,
            srcNodata=src_nodata,
            dstNodata=0,
            creationOptions=creation_opts,
            multithread=True,
            warpOptions=["NUM_THREADS=ALL_CPUS"],
            warpMemoryLimit=warp_mem_bytes,
            callback=progress_cb,
        )
        gdal.Warp(output_path, ds_utm, options=warp_opts)
        print("\n[SUCESSO] - Warp concluído.")
    else:
        translate_opts = gdal.TranslateOptions(
            format="GTiff",
            creationOptions=creation_opts,
            noData=0
        )
        gdal.Translate(output_path, ds_utm, options=translate_opts)

def gerar_mask_area_util(ds, path_out_mask, tile_size=512, th_valid=10):
    """
    Gera uma máscara binária da área útil do mosaico a partir do raster
    padronizado em escala de cinza.

    O raster é processado em tiles para reduzir o consumo de memória. Em cada
    bloco, pixels acima do limiar definido são considerados pertencentes à
    área útil do mosaico, enquanto pixels abaixo do limiar são tratados como
    fundo inválido. O resultado é salvo como um GeoTIFF binário
    georreferenciado.

    Args:
        ds (gdal.Dataset): Dataset raster em escala de cinza.
        path_out_mask (str): Caminho do GeoTIFF de saída da máscara.
        tile_size (int): Tamanho dos blocos de processamento em pixels.
        th_valid (int): Limiar de intensidade para separar área útil do fundo.

    Returns:
        None: A máscara binária é salva diretamente em disco.
    """

    band = ds.GetRasterBand(1)
    w, h = ds.RasterXSize, ds.RasterYSize
    gt = ds.GetGeoTransform()

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        path_out_mask, w, h, 1, gdal.GDT_Byte,
        options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"]
    )
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(ds.GetProjection())

    out_band = out_ds.GetRasterBand(1)
    out_band.SetNoDataValue(0)

    for y0 in range(0, h, tile_size):
        ysize = min(tile_size, h - y0)
        for x0 in range(0, w, tile_size):
            xsize = min(tile_size, w - x0)

            tile_data = band.ReadAsArray(x0, y0, xsize, ysize)
            if tile_data is None:
                continue

            tile_u8 = np.clip(tile_data, 0, 255).astype(np.uint8)
            valid_mask = (tile_u8 > th_valid).astype(np.uint8) * 255

            out_band.WriteArray(valid_mask, x0, y0)

    out_band.FlushCache()
    out_band = None
    out_ds = None

def calcular_area_util_ha(mask_path, close_ksize=7, min_component_pixels=500):
    """
    Calcula a área útil total do mosaico em hectares a partir de uma máscara
    binária previamente gerada.

    A função aplica um fechamento morfológico para consolidar pequenas falhas
    internas da máscara e, em seguida, remove componentes conexos muito
    pequenos para evitar que ruídos influenciem o cálculo. A área final é
    obtida pela contagem dos pixels válidos multiplicada pela área real de
    cada pixel.

    Args:
        mask_path (str): Caminho do arquivo de máscara binária georreferenciada.
        close_ksize (int): Tamanho do kernel usado no fechamento morfológico.
        min_component_pixels (int): Área mínima, em pixels, para manter um componente conexo.

    Returns:
        float: Área útil estimada do mosaico em hectares.
    """

    ds = gdal.Open(mask_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Não foi possível abrir a máscara: {mask_path}")

    band = ds.GetRasterBand(1)
    mask = band.ReadAsArray()
    gt = ds.GetGeoTransform()
    pixel_area_m2 = abs(gt[1] * gt[5])

    mask = np.clip(mask, 0, 255).astype(np.uint8)

    if close_ksize > 1:
        kernel = np.ones((close_ksize, close_ksize), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    clean_mask = np.zeros_like(mask)
    for label in range(1, num_labels):
        area_pixels = stats[label, cv2.CC_STAT_AREA]
        if area_pixels >= min_component_pixels:
            clean_mask[labels == label] = 255

    num_pixels = int(np.count_nonzero(clean_mask))
    area_m2 = num_pixels * pixel_area_m2

    return round(area_m2 / 10000.0, 3)

def processar_tif_em_tiles(ds, path_out_mask, tile_size=512, th_valid=10):
    """
    Processa um mosaico GeoTIFF em tiles para detectar objetos e gerar uma
    máscara binária georreferenciada.

    Cada tile passa por um pipeline de filtragem, subtração de fundo,
    limiarização automática e refinamento morfológico. Durante o processo,
    é gerada uma máscara da área útil do mosaico para evitar detecções em
    regiões inválidas e permitir a remoção de contornos espúrios na transição
    entre a área útil e o fundo. Ao final, apenas objetos dentro do intervalo
    de tamanho físico definido são mantidos na máscara final.

    Args:
        ds (gdal.Dataset): Raster de entrada em escala de cinza.
        path_out_mask (str): Caminho do GeoTIFF de máscara de saída.
        tile_size (int): Tamanho do bloco de processamento em pixels.
        th_valid (int): Limiar para definição da área útil do mosaico.

    Returns:
        None: A máscara binária resultante é salva diretamente em disco.
    """
    print("\n[ETAPA] Gerando Máscara de Segmentação...")

    band = ds.GetRasterBand(1)
    w, h = ds.RasterXSize, ds.RasterYSize
    gt = ds.GetGeoTransform()

    pixel_area_m2 = abs(gt[1] * gt[5])
    min_pixels = 0.25 / pixel_area_m2
    max_pixels = 4.0 / pixel_area_m2

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        path_out_mask, w, h, 1, gdal.GDT_Byte,
        options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"]
    )
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(ds.GetProjection())

    out_band = out_ds.GetRasterBand(1)
    out_band.SetNoDataValue(0)

    kernel = np.ones((5, 5), np.uint8)
    kernel_edge = np.ones((5, 5), np.uint8)

    for y0 in range(0, h, tile_size):
        ysize = min(tile_size, h - y0)

        for x0 in range(0, w, tile_size):
            xsize = min(tile_size, w - x0)

            tile_data = band.ReadAsArray(x0, y0, xsize, ysize)
            if tile_data is None:
                continue

            tile_u8 = np.clip(tile_data, 0, 255).astype(np.uint8)

            # máscara da área útil real do mosaico
            valid_mask = (tile_u8 > th_valid).astype(np.uint8) * 255
            valid_eroded = cv2.erode(valid_mask, kernel_edge, iterations=1)
            valid_border = cv2.subtract(valid_mask, valid_eroded)

            clean = cv2.bilateralFilter(tile_u8, d=9, sigmaColor=75, sigmaSpace=75)
            bg = cv2.GaussianBlur(clean, (61, 61), 0)
            fg = cv2.subtract(bg, clean)
            blur = cv2.GaussianBlur(fg, (11, 11), 0)

            _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)

            cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            for c in cnts:
                area_c = cv2.contourArea(c)
                if area_c == 0:
                    continue

                peri = cv2.arcLength(c, True)
                circularidade = (4 * np.pi * area_c) / (peri ** 2) if peri > 0 else 0

                x_r, y_r, w_r, h_r = cv2.boundingRect(c)
                is_very_long = (w_r > xsize * 0.5) or (h_r > ysize * 0.5)

                contour_mask = np.zeros_like(th)
                cv2.drawContours(contour_mask, [c], -1, 255, thickness=1)

                touches_invalid_transition = np.any((contour_mask > 0) & (valid_border > 0))

                if touches_invalid_transition and (is_very_long or circularidade < 0.2):
                    cv2.drawContours(th, [c], -1, 0, thickness=12)

            erosao = cv2.erode(th, kernel, iterations=2)
            dilatacao = cv2.dilate(erosao, kernel, iterations=2)

            contours, _ = cv2.findContours(dilatacao, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            mask_tile = np.zeros_like(dilatacao)
            for c in contours:
                area = cv2.contourArea(c)
                if min_pixels <= area <= max_pixels:
                    cv2.drawContours(mask_tile, [c], -1, 255, -1)

            # garante que nada seja marcado fora da área útil
            mask_tile = cv2.bitwise_and(mask_tile, valid_mask)

            out_band.WriteArray(mask_tile, x0, y0)

    print('[SUCESSO] Máscara de Segmentação Gerada.')
    out_band.FlushCache()
    out_band = None
    out_ds = None

def mask_to_polygons_full(mask_path):
    """
    Converte uma máscara raster binária em polígonos georreferenciados.

    A função identifica os contornos externos presentes na máscara, converte
    as coordenadas de pixel para coordenadas geográficas com base no
    georreferenciamento do raster e cria polígonos vetoriais válidos a partir
    dessas geometrias.

    Args:
        mask_path (str): Caminho do arquivo TIFF contendo a máscara binária.

    Returns:
        tuple:
            - list: Lista de polígonos shapely gerados a partir da máscara.
            - str: WKT da projeção original do raster.
    """
    
    print("\n[ETAPA] Iniciando Detecção: Máscara -> Poligonos")

    ds = gdal.Open(mask_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"[FALHA] Não foi possível abrir a máscara: {mask_path}")

    band = ds.GetRasterBand(1)
    mask = band.ReadAsArray()
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    polygons = []
    for c in contours:
        if len(c) < 3:
            continue

        pts = c.reshape(-1, 2)
        poly_pts = []

        for pt in pts:
            px, py = pt[0], pt[1]
            gx = gt[0] + px * gt[1] + py * gt[2]
            gy = gt[3] + px * gt[4] + py * gt[5]
            poly_pts.append((gx, gy))

        if len(poly_pts) >= 3:
            poly = Polygon(poly_pts)
            if poly.is_valid and not poly.is_empty:
                polygons.append(poly)

    print("[SUCESSO] Fim da Conversão.")
    ds = None
    return polygons, proj

def salvar_geometrias_geojson(polygons, src_wkt, output_path):
    """
    Exporta os polígonos detectados para o formato GeoJSON georreferenciado.
    Converte as geometrias do sistema métrico UTM (utilizado para processamento) 
    para o padrão global WGS84 (EPSG:4326).

    Args:
        polygons (list): Lista de objetos shapely.geometry.Polygon.
        src_wkt (str): Projeção de origem das geometrias.
        output_path (str): Caminho de destino do arquivo .geojson.
    """

    if not polygons:
        print("Nenhum polígono encontrado para salvar.")
        return

    gdf = gpd.GeoDataFrame({'geometry': polygons}, crs=src_wkt)    
    gdf['area_m2'] = gdf.geometry.area
    gdf_4326 = gdf.to_crs(epsg=4326)
    gdf_4326.to_file(output_path, driver='GeoJSON')

    print(f"[SUCESSO] GeoJSON salvo em: {output_path}")

def calcular_metricas_homogeneidade(gdf):
    """
    Avalia a homogeneidade do plantio através da análise da área das copas.
    Calcula o Coeficiente de Variação (CV) e distribuições por quartis para 
    prover um indicador qualitativo (Alta, Média ou Baixa homogeneidade) 
    sobre quão similar é o desenvolvimento das mudas.

    Args:
        gdf (geopandas.GeoDataFrame): DataFrame contendo as geometrias detectadas.

    Returns:
        dict: Indicadores estatísticos de similaridade de tamanho.
    """

    print("\n[ETAPA] Calculando Métricas da Plantação")

    if gdf.empty:
        return {}

    areas = gdf['area_m2']
    
    mean = float(areas.mean())
    standard_deviation = float(areas.std())

    cv = (standard_deviation / mean) * 100 if mean > 0 else 0
    
    q1 = float(areas.quantile(0.25))
    q3 = float(areas.quantile(0.75))
    
    print("[SUCESSO] Métricas Calculadas.")
    return {
        "media_tamanho_m2": round(mean, 4),
        "desvio_padrao_m2": round(standard_deviation, 4),
        "coeficiente_variacao_percentual": round(cv, 2),
        "quartil_inferior_m2": round(q1, 4),
        "quartil_superior_m2": round(q3, 4),
        "homogeneidade_classificacao": "Alta" if cv < 15 else "Media" if cv < 30 else "Baixa"
    }

def limpar_pasta(path):
    """
    Remove todos os arquivos e subpastas dentro de um diretório,
    preservando a própria pasta.

    Args:
        path (str): Caminho da pasta a ser limpa.
    """
    if not os.path.exists(path):
        return

    for item in os.listdir(path):
        item_path = os.path.join(path, item)

        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        except Exception as e:
            print(f"[AVISO] Falha ao remover {item_path}: {e}")

input_dir = config['paths']['input']
temp_dir = config['paths']['temp']
output_dir = config['paths']['output']

# --- EXECUÇÃO PRINCIPAL ---
def main():
    
    os.makedirs(output_dir, exist_ok=True)

    mosaics = os.listdir(input_dir)
    mosaics = [os.path.join(input_dir, mosaic) for mosaic in mosaics if mosaic.endswith('.tif')]
    
    print("\n[ETAPA] Limpando diretórios de execução...")

    limpar_pasta(output_dir)
    limpar_pasta(temp_dir)

    for mosaic in mosaics:
        basename = os.path.splitext(os.path.basename(mosaic))[0]
        print(f"\n--- Iniciando: {basename} ---")

        ds = None
        ds_utm = None
        ds_pad = None

        p = {
            "escala_cinza": os.path.join(config["paths"]["temp"], f"{basename}_gray.tif"),
            "padronizado": os.path.join(config["paths"]["temp"], f"{basename}_pad.tif"),
            "mask": os.path.join(config["paths"]["temp"], f"mask_{basename}.tif"),
            "area_util": os.path.join(config["paths"]["temp"], f"area_util_{basename}.tif"),
            "geojson": os.path.join(config["paths"]["output"], f"{basename}.geojson"),
            "json": os.path.join(config["paths"]["output"], f"{basename}_stats.json")
        }

        try:
            ds = gdal.Open(mosaic, gdal.GA_ReadOnly)
            if ds is None:
                raise RuntimeError(f"Não foi possível abrir o mosaico: {mosaic}")
            
            ensure_grayscale(ds, p["escala_cinza"])
            
            ds_gray = gdal.Open(p["escala_cinza"], gdal.GA_ReadOnly)
            if ds_gray is None:
                raise RuntimeError("Não foi possível abrir o TIFF em escala de cinza.")

            ds_utm = ensure_epsg(ds_gray, config["target_epsg_code"])
            print(f"[SUCESSO] Sistema de coordenadas padronizado.")

            padronizar_gsd(
                ds_utm,
                _build_creation_opts(),
                _get_warp_memory_bytes(config['warp_memory_gb']),
                p["padronizado"]
            )

            ds_pad = gdal.Open(p["padronizado"], gdal.GA_ReadOnly)
            if ds_pad is None:
                raise RuntimeError("Não foi possível abrir o TIFF padronizado.")

            gerar_mask_area_util(ds_pad, p['area_util'])
            area_ha = calcular_area_util_ha(p['area_util'])

            processar_tif_em_tiles(ds_pad, p["mask"])
            polygons, wkt_utm = mask_to_polygons_full(p["mask"])

            gdf = gpd.GeoDataFrame({'geometry': polygons}, crs=wkt_utm)
            gdf['area_m2'] = gdf.geometry.area

            metricas_h = calcular_metricas_homogeneidade(gdf)

            gdf.to_crs(epsg=4326).to_file(p["geojson"], driver='GeoJSON')

            stats = {
                "mosaico": basename,
                "area_ha": area_ha,
                "total_plantas": len(gdf),
                "plantas_por_ha": round(len(gdf) / area_ha, 2) if area_ha > 0 else 0,
                "homogeneidade": metricas_h
            }

            with open(p["json"], 'w') as f:
                json.dump(stats, f, indent=4)

            print(f"\n[SUCESSO] Processamento concluído para {basename}")

        except Exception as e:
            print(f"\n[FALHA] Erro ao processar {mosaic}: {e}")

        finally:
            ds_pad = None
            ds_utm = None
            ds_gray = None
            ds = None

            print("\n[ETAPA] Limpando arquivos intermediários e liberando memória...")
            limpar_pasta(temp_dir)


if __name__ == '__main__':
    main()
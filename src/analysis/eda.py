"""
EDA orientado a KPI para SmartEstate.

Este modulo no describe columnas: mide si la cascada de recomendacion definida
en `overview.md` es viable con los datos disponibles.

    Nivel 1  ->  filtro tipo + modalidad      bloque 1
    Nivel 2  ->  BallTree geografico          bloque 2
    Nivel 3A ->  sim(A,B) tabular + NLP       bloque 3
    Nivel 3B ->  f(r) por precio/m2           bloque 4
    Nivel 3C ->  k-means para diversidad      bloque 5

Reglas del modulo:
  - Ninguna funcion imprime ni grafica: todas devuelven DataFrames.
  - Ninguna funcion muta su entrada ni escribe en `data/`.
  - El EDA solo mide. Imputar, recortar y descartar es tarea de la limpieza.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Constantes de dominio
# --------------------------------------------------------------------------

RUTA_DATOS = Path("data")

RADIO_TIERRA_KM = 6371.0

#: Categoria de inmueble, segun `data/metadatos.pdf`. Es la clave del filtro de Nivel 1.
SECTORES = {9: "Industrial", 11: "Office", 12: "Retail", 15: "Land"}

#: Estructura del espacio, segun `data/metadatos.pdf`. No es taxonomia de negocio.
#: El PDF acota el ejercicio a Single y Subspace: un Complex es el contenedor
#: (el centro comercial entero), no una unidad arrendable.
TIPOS_ESPACIO = {1: "Single", 2: "Complex", 3: "Subspace"}

#: Clave de particion sobre la que operan los Niveles 1, 2 y 3.
PARTICION = ["sector_id", "intencion"]

#: Atributos fisicos declarados como especificos de nave industrial en el PDF.
ATRIBUTOS_INDUSTRIALES = ["luz_natural", "luminarias", "puertos_carga", "material_piso"]

#: Minimo de candidatos para considerar que una consulta es servible (Top-10).
MIN_CANDIDATOS = 10

#: Radios de expansion del BallTree, en km (overview.md, Nivel 2).
RADIOS_KM = (5.0, 10.0, 15.0)

#: Caja envolvente de Mexico continental, para validar coordenadas.
BBOX_MEXICO = {"lat_min": 14.0, "lat_max": 33.0, "lon_min": -118.5, "lon_max": -86.0}

#: Nombres canonicos de la tabla de publicaciones.
COLUMNAS_SPOTS = {
    "Spot ID": "spot_id",
    "Spot Sector ID": "sector_id",
    "Spot Type ID": "tipo_id",
    "Spot Settlement": "colonia",
    "Spot Municipality": "municipio",
    "Spot State": "estado",
    "Spot Region": "region",
    "Spot Corridor": "corredor",
    "Spot Address": "direccion",
    "Spot Title": "titulo",
    "Spot Description": "descripcion",
    "Spot Latitude": "lat",
    "Spot Longitude": "lon",
    "Spot Area In Sqm": "area_m2",
    "Spot Price Sqm Mxn Rent": "renta_m2",
    "Spot Price Total Mxn Rent": "renta_total",
    "Spot Price Sqm Mxn Sale": "venta_m2",
    "Spot Price Total Mxn Sale": "venta_total",
    "Spot Maintenance Cost Mxn ($)": "mantenimiento",
    "Spot Modality": "modalidad",
    "User ID": "user_id",
    "Spot Created Date": "fecha_alta",
}

#: Nombres canonicos de la tabla de atributos fisicos.
COLUMNAS_ATRIBUTOS = {
    "spot_id": "spot_id",
    "natural_light": "luz_natural",
    "luminaries": "luminarias",
    "charging_ports": "puertos_carga",
    "security_type": "tipo_seguridad",
    "floor_level_number": "piso",
    "number_of_elevators": "elevadores",
    "vertical_height": "altura",
    "parking_spaces": "estacionamientos",
    "building_status": "estado_edificio",
    "floor_material": "material_piso",
}

_NUMERICAS_SPOTS = [
    "area_m2",
    "renta_m2",
    "renta_total",
    "venta_m2",
    "venta_total",
    "mantenimiento",
]

#: Modalidad publicada -> intenciones de lead que puede atender.
#: Un inmueble "Rent & Sale" es candidato valido para un lead de renta Y uno de venta.
_MODALIDAD_A_INTENCIONES = {
    "Rent": ("Rent",),
    "Sale": ("Sale",),
    "Rent & Sale": ("Rent", "Sale"),
}


# --------------------------------------------------------------------------
# Bloque 0 - Carga y contrato de datos
# --------------------------------------------------------------------------


def _a_numero(serie: pd.Series) -> pd.Series:
    """Convierte texto con separador de miles ('1,234.5') a float."""
    limpio = serie.astype("string").str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(limpio, errors="coerce")


def _a_grados(serie: pd.Series) -> pd.Series:
    """Convierte coordenadas con hemisferio ('99.20000000 W') a grados con signo."""
    texto = serie.astype("string").str.strip()
    magnitud = pd.to_numeric(texto.str.extract(r"([0-9]+\.?[0-9]*)", expand=False), errors="coerce")
    hemisferio = texto.str.extract(r"([NSEWO])\s*$", expand=False)
    signo = np.where(hemisferio.isin(["S", "W", "O"]), -1.0, 1.0)
    return magnitud * signo


def _decimales_significativos(serie: pd.Series) -> pd.Series:
    """Cuenta decimales reales (sin ceros de relleno) de una coordenada textual."""
    parte = serie.astype("string").str.extract(r"[0-9]+\.([0-9]+)", expand=False)
    return parte.fillna("").str.rstrip("0").str.len()


def cargar_spots(ruta: Path | str = RUTA_DATOS, archivo: str = "data.csv") -> pd.DataFrame:
    """Lee las publicaciones y aplica el contrato de tipos. No imputa ni descarta."""
    crudo = pd.read_csv(Path(ruta) / archivo, dtype=str)
    df = crudo.rename(columns=COLUMNAS_SPOTS)

    df["spot_id"] = _a_numero(df["spot_id"]).astype("Int64")
    df["sector_id"] = _a_numero(df["sector_id"]).astype("Int64")
    df["tipo_id"] = _a_numero(df["tipo_id"]).astype("Int64")

    for columna in _NUMERICAS_SPOTS:
        df[columna] = _a_numero(df[columna])

    df["lat_decimales"] = _decimales_significativos(df["lat"])
    df["lat"] = _a_grados(df["lat"])
    df["lon"] = _a_grados(df["lon"])
    df["fecha_alta"] = pd.to_datetime(df["fecha_alta"], format="%B %d, %Y", errors="coerce")

    return df


def cargar_atributos(ruta: Path | str = RUTA_DATOS, archivo: str = "data_at.csv") -> pd.DataFrame:
    """Lee los atributos fisicos y aplica el contrato de tipos."""
    crudo = pd.read_csv(Path(ruta) / archivo, dtype=str)
    df = crudo.rename(columns=COLUMNAS_ATRIBUTOS)

    df["spot_id"] = _a_numero(df["spot_id"]).astype("Int64")
    for columna in ["luz_natural", "puertos_carga", "piso", "elevadores", "altura",
                    "estacionamientos", "estado_edificio"]:
        df[columna] = _a_numero(df[columna])

    return df


def cargar_datos(ruta: Path | str = RUTA_DATOS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Devuelve (spots, atributos) ya tipados."""
    return cargar_spots(ruta), cargar_atributos(ruta)


def perfil_columnas(df: pd.DataFrame) -> pd.DataFrame:
    """Perfil por columna: tipo, vacios, cardinalidad y un ejemplo real."""
    filas = []
    for columna in df.columns:
        serie = df[columna]
        no_nulos = serie.dropna()
        filas.append({
            "columna": columna,
            "tipo": str(serie.dtype),
            "pct_nulos": round(serie.isna().mean() * 100, 2),
            "n_unicos": int(serie.nunique(dropna=True)),
            "ejemplo": no_nulos.iloc[0] if len(no_nulos) else pd.NA,
        })
    return pd.DataFrame(filas).sort_values("pct_nulos", ascending=False, ignore_index=True)


def nulos_estructurales(spots: pd.DataFrame) -> pd.DataFrame:
    """
    Separa vacios reales de vacios por diseno.

    Un inmueble en renta no tiene precio de venta: ese vacio no es un defecto
    de calidad, es que la columna no aplica. Confundirlos infla el diagnostico.
    """
    filas = []
    for columna, modalidades in [
        ("renta_m2", ["Rent", "Rent & Sale"]),
        ("renta_total", ["Rent", "Rent & Sale"]),
        ("venta_m2", ["Sale", "Rent & Sale"]),
        ("venta_total", ["Sale", "Rent & Sale"]),
    ]:
        aplica = spots["modalidad"].isin(modalidades)
        filas.append({
            "columna": columna,
            "pct_nulos_bruto": round(spots[columna].isna().mean() * 100, 2),
            "n_aplica": int(aplica.sum()),
            "pct_nulos_donde_aplica": round(spots.loc[aplica, columna].isna().mean() * 100, 2),
            "pct_llenos_donde_no_aplica": round(spots.loc[~aplica, columna].notna().mean() * 100, 2),
        })
    return pd.DataFrame(filas)


def recuperabilidad_jerarquica(spots: pd.DataFrame, hijo: str, padre: str) -> pd.DataFrame:
    """
    Mide cuantos vacios de `padre` pueden deducirse a partir de `hijo`.

    Ejemplo: el estado falta en muchas filas, pero el municipio nunca falta y
    casi siempre determina el estado. Descartar la columna es perder informacion
    que esta en la tabla.
    """
    mapa = spots.dropna(subset=[padre]).groupby(hijo)[padre].nunique()
    univocos = set(mapa[mapa == 1].index)
    faltantes = spots[spots[padre].isna()]
    recuperables = int(faltantes[hijo].isin(univocos).sum())
    n_faltantes = len(faltantes)
    return pd.DataFrame([{
        "padre": padre,
        "hijo": hijo,
        "n_faltantes_padre": n_faltantes,
        "n_recuperables_via_hijo": recuperables,
        "pct_recuperable": round(recuperables / n_faltantes * 100, 2) if n_faltantes else 0.0,
        "hijos_ambiguos": int((mapa > 1).sum()),
    }])



def taxonomia_declarada(spots: pd.DataFrame) -> pd.DataFrame:
    """
    Contrasta los codigos observados contra el diccionario de `data/metadatos.pdf`.

    Un codigo presente en los datos y ausente del diccionario es deuda de
    documentacion, no un dato invalido: hay que resolverlo antes de que el
    Nivel 1 filtre sobre una categoria cuyo significado nadie confirmo.
    """
    filas = []
    for columna, diccionario in [("sector_id", SECTORES), ("tipo_id", TIPOS_ESPACIO)]:
        conteo = spots[columna].value_counts().sort_index()
        for codigo, n in conteo.items():
            filas.append({
                "columna": columna,
                "codigo": int(codigo),
                "significado": diccionario.get(int(codigo), "SIN DOCUMENTAR"),
                "n": int(n),
                "pct": round(n / len(spots) * 100, 2),
                "documentado": int(codigo) in diccionario,
            })
        for codigo, nombre in diccionario.items():
            if codigo not in set(conteo.index):
                filas.append({
                    "columna": columna,
                    "codigo": codigo,
                    "significado": nombre,
                    "n": 0,
                    "pct": 0.0,
                    "documentado": True,
                })
    return pd.DataFrame(filas).sort_values(["columna", "codigo"], ignore_index=True)


def estructura_espacio(spots: pd.DataFrame) -> pd.DataFrame:
    """
    Desglosa el inventario por estructura del espacio dentro de cada sector.

    `metadatos.pdf` acota el ejercicio a espacios Single y Subspace. Un Complex
    es el contenedor -el centro comercial completo, el edificio entero- y su
    area es aproximada: recomendarlo como si fuera una unidad arrendable mezcla
    dos cosas distintas y castiga el LQS.
    """
    tabla = pd.crosstab(spots["sector_id"], spots["tipo_id"])
    tabla.columns = [TIPOS_ESPACIO.get(int(c), str(c)) for c in tabla.columns]
    tabla = tabla.reset_index()
    tabla.insert(1, "sector", tabla["sector_id"].map(SECTORES).fillna("SIN DOCUMENTAR"))
    tabla["n"] = tabla[[c for c in tabla.columns if c in TIPOS_ESPACIO.values()]].sum(axis=1)
    tabla["pct_complex"] = round(tabla.get("Complex", 0) / tabla["n"] * 100, 2)
    return tabla


def cobertura_tabular_por_sector(
    spots: pd.DataFrame,
    atributos: pd.DataFrame,
    umbral: float = 50.0,
) -> pd.DataFrame:
    """
    Cobertura de cada atributo fisico dentro de cada sector.

    Medir esto en global es enganoso: `metadatos.pdf` declara luz natural,
    luminarias, puertos de carga y material de piso como campos de nave
    industrial. Su vacio en una oficina no es un dato faltante, es una columna
    que no aplica -el mismo error que confundir el nulo estructural de precio
    de venta con un problema de calidad.
    """
    unido = spots[["spot_id", "sector_id", "area_m2", "mantenimiento"]].merge(
        atributos, on="spot_id", how="left"
    )
    candidatas = [c for c in unido.columns if c not in ("spot_id", "sector_id")]

    filas = []
    for sector_id, grupo in unido.groupby("sector_id", observed=True):
        for columna in candidatas:
            serie = grupo[columna]
            no_nulos = serie.dropna()
            filas.append({
                "sector_id": int(sector_id),
                "sector": SECTORES.get(int(sector_id), "SIN DOCUMENTAR"),
                "columna": columna,
                "pct_cobertura": round(serie.notna().mean() * 100, 2),
                "n_unicos": int(no_nulos.nunique()),
                "usable": bool(serie.notna().mean() * 100 >= umbral and no_nulos.nunique() > 1),
            })

    return pd.DataFrame(filas).sort_values(
        ["sector_id", "pct_cobertura"], ascending=[True, False], ignore_index=True
    )


def dimensiones_usables_por_sector(
    spots: pd.DataFrame,
    atributos: pd.DataFrame,
    umbral: float = 50.0,
    objetivo: int = 5,
) -> pd.DataFrame:
    """Resume cuantas dimensiones tabulares reales tiene cada sector, y cuales."""
    detalle = cobertura_tabular_por_sector(spots, atributos, umbral=umbral)
    usables = detalle[detalle["usable"]]
    resumen = (
        usables.groupby(["sector_id", "sector"], observed=True)
        .agg(n_dimensiones=("columna", "size"), columnas=("columna", lambda s: ", ".join(s)))
        .reset_index()
    )
    resumen["alcanza_objetivo"] = resumen["n_dimensiones"] >= objetivo
    return resumen.sort_values("n_dimensiones", ascending=False, ignore_index=True)


# --------------------------------------------------------------------------
# Bloque 1 - Nivel 1: filtro por tipo de inmueble y modalidad
# --------------------------------------------------------------------------


def etiquetar_sector(df: pd.DataFrame) -> pd.DataFrame:
    """Anade el nombre de negocio del sector junto a su codigo."""
    salida = df.copy()
    salida.insert(
        salida.columns.get_loc("sector_id") + 1,
        "sector",
        salida["sector_id"].map(SECTORES).fillna("SIN DOCUMENTAR"),
    )
    return salida


def expandir_por_intencion(spots: pd.DataFrame) -> pd.DataFrame:
    """
    Une cada inmueble a las intenciones de lead que puede atender.

    Devuelve una fila por (inmueble, intencion), de modo que un 'Rent & Sale'
    aparece dos veces. Esta es la particion real sobre la que opera el Nivel 1.
    """
    piezas = []
    for modalidad, intenciones in _MODALIDAD_A_INTENCIONES.items():
        bloque = spots[spots["modalidad"] == modalidad]
        for intencion in intenciones:
            piezas.append(bloque.assign(intencion=intencion))
    return pd.concat(piezas, ignore_index=True)


def matriz_particiones(spots: pd.DataFrame, min_n: int = MIN_CANDIDATOS) -> pd.DataFrame:
    """
    Inventario disponible por particion (tipo x intencion) y veredicto de viabilidad.

    Una particion es viable si tiene al menos `min_n` + 1 inmuebles: el de
    referencia mas los `min_n` candidatos del Top-N.
    """
    expandido = expandir_por_intencion(spots)
    tabla = (
        expandido.groupby(PARTICION, observed=True)
        .size()
        .reset_index(name="n_inventario")
    )
    tabla["n_candidatos_max"] = tabla["n_inventario"] - 1
    tabla["viable"] = tabla["n_candidatos_max"] >= min_n
    tabla["pct_inventario"] = round(tabla["n_inventario"] / len(spots) * 100, 2)
    return tabla.sort_values("n_inventario", ignore_index=True)


def particiones_inviables(spots: pd.DataFrame, min_n: int = MIN_CANDIDATOS) -> pd.DataFrame:
    """Solo las particiones que no pueden llenar un Top-N ni ignorando la geografia."""
    tabla = matriz_particiones(spots, min_n=min_n)
    return tabla[~tabla["viable"]].reset_index(drop=True)


def cobertura_filtro_nivel1(spots: pd.DataFrame, min_n: int = MIN_CANDIDATOS) -> pd.DataFrame:
    """Porcentaje de leads que caen en una particion servible, antes de geografia."""
    tabla = matriz_particiones(spots, min_n=min_n)
    servibles = int(tabla.loc[tabla["viable"], "n_inventario"].sum())
    total = int(tabla["n_inventario"].sum())
    return pd.DataFrame([{
        "min_candidatos": min_n,
        "n_particiones": len(tabla),
        "n_particiones_inviables": int((~tabla["viable"]).sum()),
        "pct_leads_servibles": round(servibles / total * 100, 2),
    }])


# --------------------------------------------------------------------------
# Bloque 2 - Nivel 2: viabilidad del BallTree geografico
# --------------------------------------------------------------------------


def validar_coordenadas(spots: pd.DataFrame) -> pd.DataFrame:
    """
    Diagnostico de la senal geografica: la unica entrada del Nivel 2.

    La resolucion importa tanto como la completitud: coordenadas truncadas a
    dos decimales describen una celda de ~1.1 km, asi que cualquier distancia
    por debajo de ese orden es ruido de redondeo, no cercania real.
    """
    n = len(spots)
    dentro = (
        spots["lat"].between(BBOX_MEXICO["lat_min"], BBOX_MEXICO["lat_max"])
        & spots["lon"].between(BBOX_MEXICO["lon_min"], BBOX_MEXICO["lon_max"])
    )
    unicas = spots[["lat", "lon"]].drop_duplicates().shape[0]
    decimales = spots["lat_decimales"]
    grado_km = 111.32
    resolucion_km = grado_km * (10.0 ** -decimales.median())

    return pd.DataFrame([{
        "n_registros": n,
        "pct_nulos": round(spots[["lat", "lon"]].isna().any(axis=1).mean() * 100, 2),
        "pct_fuera_de_mexico": round((~dentro).mean() * 100, 2),
        "n_coordenadas_unicas": unicas,
        "pct_coordenada_compartida": round((1 - unicas / n) * 100, 2),
        "decimales_mediana": int(decimales.median()),
        "resolucion_aprox_km": round(resolucion_km, 2),
    }])


def _haversine_km(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Matriz de distancias haversine (km) entre todos los puntos dados."""
    lat_rad = np.radians(lat)[:, None]
    lon_rad = np.radians(lon)[:, None]
    d_lat = lat_rad - lat_rad.T
    d_lon = lon_rad - lon_rad.T
    a = np.sin(d_lat / 2) ** 2 + np.cos(lat_rad) * np.cos(lat_rad.T) * np.sin(d_lon / 2) ** 2
    return 2 * RADIO_TIERRA_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def densidad_vecinos(
    spots: pd.DataFrame,
    radios_km: tuple[float, ...] = RADIOS_KM,
) -> pd.DataFrame:
    """
    Cuenta, por inmueble, cuantos candidatos reales tiene a cada radio.

    Los vecinos se cuentan solo dentro de la misma particion (tipo x intencion),
    porque el Nivel 2 opera despues del filtro del Nivel 1: un vecino de otro
    tipo de inmueble no es un candidato.
    """
    expandido = expandir_por_intencion(spots)
    expandido = expandido.dropna(subset=["lat", "lon"])
    piezas = []

    for (sector, intencion), grupo in expandido.groupby(PARTICION, observed=True):
        distancias = _haversine_km(grupo["lat"].to_numpy(), grupo["lon"].to_numpy())
        np.fill_diagonal(distancias, np.inf)  # el inmueble de referencia no es su candidato
        conteos = {f"vecinos_{int(r)}km": (distancias <= r).sum(axis=1) for r in radios_km}
        piezas.append(pd.DataFrame({
            "spot_id": grupo["spot_id"].to_numpy(),
            "sector_id": sector,
            "intencion": intencion,
            "n_particion": len(grupo),
            **conteos,
        }))

    return pd.concat(piezas, ignore_index=True)


def cobertura_radio(
    densidad: pd.DataFrame,
    min_n: int = MIN_CANDIDATOS,
    radios_km: tuple[float, ...] = RADIOS_KM,
) -> pd.DataFrame:
    """% de consultas que alcanzan `min_n` candidatos a cada radio de expansion."""
    filas = []
    for radio in radios_km:
        columna = f"vecinos_{int(radio)}km"
        filas.append({
            "radio_km": radio,
            "pct_alcanza_min_n": round((densidad[columna] >= min_n).mean() * 100, 2),
            "pct_sin_ningun_vecino": round((densidad[columna] == 0).mean() * 100, 2),
            "mediana_vecinos": float(densidad[columna].median()),
        })
    return pd.DataFrame(filas)


def cobertura_radio_por_particion(
    densidad: pd.DataFrame,
    min_n: int = MIN_CANDIDATOS,
    radio_km: float = max(RADIOS_KM),
) -> pd.DataFrame:
    """Cobertura al radio tope, desglosada por particion: donde falla el sistema."""
    columna = f"vecinos_{int(radio_km)}km"
    tabla = (
        densidad.groupby(PARTICION, observed=True)
        .agg(
            n_consultas=("spot_id", "size"),
            mediana_vecinos=(columna, "median"),
            pct_alcanza_min_n=(columna, lambda s: round((s >= min_n).mean() * 100, 2)),
        )
        .reset_index()
    )
    return tabla.sort_values("pct_alcanza_min_n", ignore_index=True)


# --------------------------------------------------------------------------
# Bloque 3 - Nivel 3A: insumos de sim(A,B)
# --------------------------------------------------------------------------


def cobertura_texto(spots: pd.DataFrame, minimo_util: int = 40) -> pd.DataFrame:
    """
    Cobertura del vector NLP (384d) por particion.

    Sin titulo ni descripcion no hay embedding, y sin embedding el candidato
    entra al ranking con la mitad de la similitud en cero: compite en desventaja
    por un defecto del dato, no por ser peor opcion.
    """
    df = expandir_por_intencion(spots)
    titulo = df["titulo"].fillna("").str.strip()
    descripcion = df["descripcion"].fillna("").str.strip()
    df = df.assign(
        _largo_texto=(titulo + " " + descripcion).str.len(),
        _sin_descripcion=descripcion.eq(""),
        _texto_pobre=(titulo + " " + descripcion).str.len() < minimo_util,
    )
    return (
        df.groupby(PARTICION, observed=True)
        .agg(
            n=("spot_id", "size"),
            pct_sin_descripcion=("_sin_descripcion", lambda s: round(s.mean() * 100, 2)),
            pct_texto_pobre=("_texto_pobre", lambda s: round(s.mean() * 100, 2)),
            mediana_caracteres=("_largo_texto", "median"),
        )
        .reset_index()
        .sort_values("pct_sin_descripcion", ascending=False, ignore_index=True)
    )


def cobertura_tabular(spots: pd.DataFrame, atributos: pd.DataFrame) -> pd.DataFrame:
    """
    Candidatas al vector tabular (~5 dimensiones), ordenadas por utilidad real.

    Una columna solo sirve si esta presente Y varia: `luminaries` puede estar
    en la tabla y no aportar nada si todos los valores no nulos son iguales.
    """
    unido = spots[["spot_id", "area_m2", "mantenimiento"]].merge(
        atributos, on="spot_id", how="left"
    )
    candidatas = [c for c in unido.columns if c != "spot_id"]

    filas = []
    for columna in candidatas:
        serie = unido[columna]
        no_nulos = serie.dropna()
        numerica = pd.api.types.is_numeric_dtype(serie)
        filas.append({
            "columna": columna,
            "pct_cobertura": round(serie.notna().mean() * 100, 2),
            "n_unicos": int(no_nulos.nunique()),
            "constante": bool(no_nulos.nunique() <= 1),
            "numerica_directa": numerica,
            "cv": round(float(no_nulos.std() / no_nulos.mean()), 3)
            if numerica and len(no_nulos) > 1 and no_nulos.mean() else np.nan,
        })

    tabla = pd.DataFrame(filas)
    tabla["usable"] = (tabla["pct_cobertura"] >= 50) & (~tabla["constante"])
    return tabla.sort_values("pct_cobertura", ascending=False, ignore_index=True)


def duplicados_texto(spots: pd.DataFrame) -> pd.DataFrame:
    """
    Texto repetido literal entre publicaciones.

    Descripciones identicas producen embeddings identicos: el coseno los declara
    gemelos y el Top-N se llena de la misma oferta repetida.
    """
    filas = []
    for columna in ["titulo", "descripcion"]:
        serie = spots[columna].fillna("").str.strip().str.lower()
        serie = serie[serie != ""]
        conteo = serie.value_counts()
        repetidos = conteo[conteo > 1]
        filas.append({
            "columna": columna,
            "n_con_texto": int(len(serie)),
            "n_textos_unicos": int(serie.nunique()),
            "pct_en_texto_repetido": round(int(repetidos.sum()) / len(serie) * 100, 2),
            "max_repeticiones": int(conteo.max()),
        })
    return pd.DataFrame(filas)


# --------------------------------------------------------------------------
# Bloque 4 - Nivel 3B: viabilidad de f(r) sobre precio por m2
# --------------------------------------------------------------------------


def _precio_m2_por_intencion(expandido: pd.DataFrame) -> pd.Series:
    """Precio por m2 correspondiente a la intencion de cada fila."""
    return np.where(
        expandido["intencion"].eq("Rent"), expandido["renta_m2"], expandido["venta_m2"]
    )


def consistencia_precio(spots: pd.DataFrame, tolerancia: float = 0.01) -> pd.DataFrame:
    """
    Verifica la identidad `precio_total = precio_m2 x area` en cada modalidad.

    Si la identidad no se cumple, una de las dos columnas esta corrupta y hay
    que decidir cual alimenta a f(r). El EDA anterior nunca ejecuto esta prueba
    y por eso confundio precio por m2 con precio total.
    """
    filas = []
    for etiqueta, p_m2, total, modalidades in [
        ("renta", "renta_m2", "renta_total", ["Rent", "Rent & Sale"]),
        ("venta", "venta_m2", "venta_total", ["Sale", "Rent & Sale"]),
    ]:
        sub = spots[spots["modalidad"].isin(modalidades)]
        evaluable = sub[[p_m2, total, "area_m2"]].dropna()
        esperado = evaluable[p_m2] * evaluable["area_m2"]
        error = (evaluable[total] - esperado).abs() / evaluable[total].replace(0, np.nan)
        filas.append({
            "modalidad": etiqueta,
            "n_evaluable": len(evaluable),
            "pct_identidad_ok": round(error.le(tolerancia).mean() * 100, 2),
            "error_relativo_mediano": round(float(error.median()), 4),
        })
    return pd.DataFrame(filas)


def rango_precio_m2(spots: pd.DataFrame) -> pd.DataFrame:
    """Percentiles del precio por m2: detecta valores fuera de escala de mercado."""
    expandido = expandir_por_intencion(spots)
    expandido = expandido.assign(precio_m2=_precio_m2_por_intencion(expandido))
    return (
        expandido.dropna(subset=["precio_m2"])
        .groupby("intencion", observed=True)["precio_m2"]
        .describe(percentiles=[0.01, 0.25, 0.5, 0.75, 0.99])
        .round(1)
        .reset_index()
    )


def outliers_precio(spots: pd.DataFrame, k: float = 1.5) -> pd.DataFrame:
    """
    Cuantifica outliers de precio/m2 por particion via IQR sobre escala log.

    Solo mide y marca: el recorte se decide en la etapa de limpieza.
    """
    expandido = expandir_por_intencion(spots)
    expandido = expandido.assign(precio_m2=_precio_m2_por_intencion(expandido))
    expandido = expandido.dropna(subset=["precio_m2"])
    expandido = expandido[expandido["precio_m2"] > 0]

    filas = []
    for (sector, intencion), grupo in expandido.groupby(PARTICION, observed=True):
        log_precio = np.log(grupo["precio_m2"])
        q1, q3 = log_precio.quantile([0.25, 0.75])
        iqr = q3 - q1
        bajo, alto = q1 - k * iqr, q3 + k * iqr
        marcados = (log_precio < bajo) | (log_precio > alto)
        filas.append({
            "sector_id": sector,
            "intencion": intencion,
            "n": len(grupo),
            "n_outliers": int(marcados.sum()),
            "pct_outliers": round(marcados.mean() * 100, 2),
            "limite_inferior": round(float(np.exp(bajo)), 1),
            "limite_superior": round(float(np.exp(alto)), 1),
            "max_observado": round(float(grupo["precio_m2"].max()), 1),
        })
    return pd.DataFrame(filas).sort_values("pct_outliers", ascending=False, ignore_index=True)


def distribucion_ratio_precio(
    spots: pd.DataFrame,
    radio_km: float = 10.0,
    max_pares: int = 200_000,
    semilla: int = 42,
) -> pd.DataFrame:
    """
    Distribucion de r = P_m2(B) / P_m2(A) entre pares que el ranker vera de verdad.

    Solo se comparan pares dentro de la misma particion y dentro del radio, que
    es exactamente el conjunto que llega al Nivel 3. Sirve para calibrar la cota
    [0.7, 1.1] de f(r): si casi todos los pares caen fuera del rango neutro, la
    cota se satura y el precio deja de discriminar.
    """
    rng = np.random.default_rng(semilla)
    expandido = expandir_por_intencion(spots)
    expandido = expandido.assign(precio_m2=_precio_m2_por_intencion(expandido))
    expandido = expandido.dropna(subset=["precio_m2", "lat", "lon"])
    expandido = expandido[expandido["precio_m2"] > 0]

    ratios = []
    for _, grupo in expandido.groupby(PARTICION, observed=True):
        if len(grupo) < 2:
            continue
        distancias = _haversine_km(grupo["lat"].to_numpy(), grupo["lon"].to_numpy())
        np.fill_diagonal(distancias, np.inf)
        i, j = np.nonzero(distancias <= radio_km)
        if len(i) == 0:
            continue
        if len(i) > max_pares:
            elegidos = rng.choice(len(i), size=max_pares, replace=False)
            i, j = i[elegidos], j[elegidos]
        precios = grupo["precio_m2"].to_numpy()
        ratios.append(precios[j] / precios[i])

    if not ratios:
        return pd.DataFrame()

    r = np.concatenate(ratios)
    percentiles = [1, 10, 25, 50, 75, 90, 99]
    return pd.DataFrame([{
        "n_pares": len(r),
        "radio_km": radio_km,
        **{f"p{p}": round(float(np.percentile(r, p)), 3) for p in percentiles},
        "pct_r_fuera_de_0.5_2.0": round(float(((r < 0.5) | (r > 2.0)).mean() * 100), 2),
    }])


# --------------------------------------------------------------------------
# Bloque 5 - Nivel 3C: justificacion del k-means de diversidad
# --------------------------------------------------------------------------


def tasa_clones(
    spots: pd.DataFrame,
    tolerancia_area: float = 0.05,
    radio_km: float = 1.0,
) -> pd.DataFrame:
    """
    Mide cuanto inventario es practicamente el mismo inmueble repetido.

    Se consideran clones los pares de la misma particion que estan casi en el
    mismo punto, tienen area equivalente y fueron publicados por el mismo
    usuario. Es la evidencia que justifica (o no) penalizar por cluster en el
    corte del Top-N: sin clones, el k-means de diversidad no resuelve nada.
    """
    expandido = expandir_por_intencion(spots).dropna(subset=["lat", "lon", "area_m2"])
    filas = []

    for (sector, intencion), grupo in expandido.groupby(PARTICION, observed=True):
        if len(grupo) < 2:
            continue
        distancias = _haversine_km(grupo["lat"].to_numpy(), grupo["lon"].to_numpy())
        np.fill_diagonal(distancias, np.inf)

        area = grupo["area_m2"].to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            dif_area = np.abs(area[:, None] - area[None, :]) / np.maximum(area[:, None], 1e-9)
        usuario = grupo["user_id"].fillna("__sin_usuario__").to_numpy()
        mismo_usuario = usuario[:, None] == usuario[None, :]

        clon = (distancias <= radio_km) & (dif_area <= tolerancia_area) & mismo_usuario
        filas.append({
            "sector_id": sector,
            "intencion": intencion,
            "n": len(grupo),
            "n_con_al_menos_un_clon": int(clon.any(axis=1).sum()),
            "pct_con_clon": round(float(clon.any(axis=1).mean() * 100), 2),
            "max_clones_de_un_inmueble": int(clon.sum(axis=1).max()),
        })

    return pd.DataFrame(filas).sort_values("pct_con_clon", ascending=False, ignore_index=True)


# --------------------------------------------------------------------------
# Veredicto consolidado
# --------------------------------------------------------------------------


def veredicto_cascada(
    spots: pd.DataFrame,
    atributos: pd.DataFrame,
    min_n: int = MIN_CANDIDATOS,
) -> pd.DataFrame:
    """Una fila por nivel de la cascada, con la metrica que decide su viabilidad."""
    cobertura_n1 = cobertura_filtro_nivel1(spots, min_n=min_n)
    densidad = densidad_vecinos(spots)
    cobertura_n2 = cobertura_radio(densidad, min_n=min_n)
    tope = cobertura_n2.iloc[-1]
    texto = cobertura_texto(spots)
    tabular = dimensiones_usables_por_sector(spots, atributos)
    identidad = consistencia_precio(spots)
    clones = tasa_clones(spots)

    sin_descripcion = spots["descripcion"].isna().mean() * 100
    n_usables = int(tabular["n_dimensiones"].min())
    peor_identidad = float(identidad["pct_identidad_ok"].min())
    pct_clones = float((clones["n_con_al_menos_un_clon"].sum() / clones["n"].sum()) * 100)

    return pd.DataFrame([
        {
            "nivel": "1 - tipo + modalidad",
            "metrica": "% de leads en particion servible",
            "valor": float(cobertura_n1.loc[0, "pct_leads_servibles"]),
            "bloqueante": int(cobertura_n1.loc[0, "n_particiones_inviables"]) > 0,
        },
        {
            "nivel": "2 - BallTree geografico",
            "metrica": f"% que alcanza {min_n} candidatos a {tope['radio_km']:.0f} km",
            "valor": float(tope["pct_alcanza_min_n"]),
            "bloqueante": float(tope["pct_alcanza_min_n"]) < 90,
        },
        {
            "nivel": "3A - sim NLP",
            "metrica": "% de inventario sin descripcion",
            "valor": round(sin_descripcion, 2),
            "bloqueante": sin_descripcion > 10,
        },
        {
            "nivel": "3A - sim tabular",
            "metrica": "dimensiones tabulares en el sector peor cubierto",
            "valor": float(n_usables),
            "bloqueante": n_usables < 5,
        },
        {
            "nivel": "3B - f(r) precio",
            "metrica": "% donde total = precio_m2 x area (peor modalidad)",
            "valor": peor_identidad,
            "bloqueante": peor_identidad < 95,
        },
        {
            "nivel": "3C - k-means diversidad",
            "metrica": "% de inventario con al menos un clon",
            "valor": round(pct_clones, 2),
            "bloqueante": False,
        },
    ])

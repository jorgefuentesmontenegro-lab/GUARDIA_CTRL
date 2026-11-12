from datetime import datetime
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from uuid import uuid4

from httpx import HTTPError
import easyocr
import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError
from storage3.utils import StorageException
from supabase import create_client


CARPETA_APP = Path(__file__).resolve().parent
ARCHIVO_REPORTES = CARPETA_APP / "reportes_estado.csv"
TAMANO_MAXIMO_FOTO = 800 * 1024
BUCKET_REPORTES = "evidencias-vehiculares"
BUCKET_ACCESOS = "evidencias-garita"
PATRON_PATENTE = re.compile(r"[A-Z]{4}\d{2}")
PERFILES = ("Guardia de garita", "Conductor operativo", "Puesto de mando")
CERROJO_REPORTES = Lock()
CAMPOS_REPORTE = (
    "fecha_reporte",
    "foto_evidencia_url",
    "kilometraje_salida",
    "luces_operativas",
    "nivel_combustible",
    "observaciones",
    "patente",
    "presion_neumaticos",
    "rut_conductor",
    "estado_operativo",
    "frenos_operativos",
)
ICONOS_ESTADO = {
    "Operativo": "🟢",
    "Requiere Mantención": "🟡",
    "Crítico": "🔴",
    "Mantenimiento urgente": "🔴",
}


def cargar_datos():
    archivos = {
        "vehiculos": "vehiculos.csv",
        "conductores": "conductores.csv",
        "autorizaciones": "autorizaciones.csv",
    }
    if all((CARPETA_APP / nombre).is_file() for nombre in archivos.values()):
        carpeta_datos = CARPETA_APP
    else:
        carpeta_datos = CARPETA_APP / "datos_demo"

    datos = {}

    for tabla, nombre_archivo in archivos.items():
        ruta = carpeta_datos / nombre_archivo
        if not ruta.is_file():
            raise FileNotFoundError(f"No se encontró el archivo requerido: {nombre_archivo}")
        datos[tabla] = pd.read_csv(ruta)

    columnas_requeridas = {
        "vehiculos": {
            "patente",
            "tipo_vehiculo",
            "marca_modelo",
            "kilometraje_actual",
            "estado_operativo",
        },
        "conductores": {"rut", "nombre_completo", "grado_o_cargo", "estado"},
        "autorizaciones": {"patente", "rut_conductor", "vigente"},
    }
    for tabla, columnas in columnas_requeridas.items():
        faltantes = columnas.difference(datos[tabla].columns)
        if faltantes:
            raise ValueError(
                f"El archivo {archivos[tabla]} no contiene las columnas: "
                f"{', '.join(sorted(faltantes))}"
            )

    datos["conductores"]["estado"] = (
        datos["conductores"]["estado"].astype(str).str.strip().str.casefold()
    )
    datos["autorizaciones"]["vigente"] = (
        datos["autorizaciones"]["vigente"]
        .astype(str)
        .str.strip()
        .str.casefold()
        .isin({"true", "1", "si", "sí"})
    )
    return datos


def normalizar_identificador(valor):
    return "".join(caracter for caracter in str(valor).upper() if caracter.isalnum())


def cargar_reportes_guardados():
    if not ARCHIVO_REPORTES.is_file():
        return pd.DataFrame(columns=CAMPOS_REPORTE)
    return pd.read_csv(ARCHIVO_REPORTES, encoding="utf-8-sig")


@st.cache_resource
def obtener_cliente_supabase():
    try:
        configuracion = st.secrets["supabase"]
        url = configuracion["url"].strip()
        clave = configuracion["key"].strip()
    except KeyError as error:
        raise RuntimeError(
            "Configura [supabase].url y [supabase].key en "
            ".streamlit/secrets.toml para guardar fotografías."
        ) from error
    if not url or not clave:
        raise RuntimeError("La dirección y la clave de Supabase no pueden estar vacías.")
    return create_client(url, clave)


def subir_fotografia(bucket, carpeta, contenido):
    if not contenido:
        return "", ""
    if len(contenido) > TAMANO_MAXIMO_FOTO:
        raise ValueError("La fotografía supera el límite de 800 KB.")

    try:
        with Image.open(BytesIO(contenido)) as imagen:
            formato = imagen.format
            imagen.verify()
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("El archivo no contiene una fotografía JPG o PNG válida.") from error

    tipos_contenido = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png")}
    if formato not in tipos_contenido:
        raise ValueError("Solo se admiten fotografías JPG o PNG.")

    extension, tipo_contenido = tipos_contenido[formato]
    nombre_objeto = f"{carpeta}/{uuid4().hex}.{extension}"
    cliente = obtener_cliente_supabase()
    cliente.storage.from_(bucket).upload(
        nombre_objeto,
        contenido,
        {
            "content-type": tipo_contenido,
            "cache-control": "3600",
            "upsert": "false",
        },
    )
    url_publica = cliente.storage.from_(bucket).get_public_url(nombre_objeto)
    return nombre_objeto, url_publica


def eliminar_fotografia(bucket, nombre_objeto):
    if nombre_objeto:
        obtener_cliente_supabase().storage.from_(bucket).remove([nombre_objeto])


def guardar_reporte_local(reporte, url_fotografia):
    fila = {
        "fecha_reporte": reporte["fecha"],
        "foto_evidencia_url": url_fotografia,
        "kilometraje_salida": reporte["kilometraje"],
        "luces_operativas": reporte["luces"],
        "nivel_combustible": reporte["combustible"],
        "observaciones": (
            f"Neumáticos: {reporte['neumaticos']}; "
            f"frenos: {reporte['frenos']}; "
            f"novedades: {reporte['novedades']}"
        ),
        "patente": reporte["patente"],
        "presion_neumaticos": reporte["neumaticos"],
        "rut_conductor": reporte["rut_conductor"],
        "estado_operativo": reporte["estado"],
        "frenos_operativos": reporte["frenos"],
    }

    archivo_temporal = None
    try:
        with CERROJO_REPORTES:
            reportes_existentes = cargar_reportes_guardados()
            columnas = list(reportes_existentes.columns)
            columnas.extend(campo for campo in CAMPOS_REPORTE if campo not in columnas)
            todos_los_reportes = pd.concat(
                [
                    reportes_existentes.reindex(columns=columnas),
                    pd.DataFrame([fila]).reindex(columns=columnas),
                ],
                ignore_index=True,
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                suffix=".tmp",
                dir=CARPETA_APP,
                delete=False,
            ) as archivo:
                archivo_temporal = Path(archivo.name)
            todos_los_reportes.to_csv(archivo_temporal, index=False, encoding="utf-8-sig")
            os.replace(archivo_temporal, ARCHIVO_REPORTES)
    finally:
        if archivo_temporal is not None:
            archivo_temporal.unlink(missing_ok=True)

    return fila


def crear_opciones_conductores(conductores):
    return {
        f"{fila.grado_o_cargo} — {fila.nombre_completo} — {fila.rut}": fila.rut
        for fila in conductores.itertuples()
    }


@st.cache_resource
def obtener_lector_ocr():
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@st.cache_data(show_spinner=False, max_entries=16)
def leer_texto_patente(contenido_foto):
    with Image.open(BytesIO(contenido_foto)) as imagen:
        imagen_corregida = ImageOps.exif_transpose(imagen).convert("RGB")
        imagen_corregida.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        imagen_datos = np.asarray(imagen_corregida)

    resultados = obtener_lector_ocr().readtext(
        imagen_datos,
        detail=1,
        paragraph=False,
        allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        decoder="greedy",
        batch_size=1,
        workers=0,
        canvas_size=1600,
        mag_ratio=1.0,
    )
    return [
        (str(resultado[1]), float(resultado[2]))
        for resultado in resultados
        if len(resultado) >= 3
    ]


def extraer_patente_detectada(resultados):
    coincidencias = []
    for texto, confianza in resultados:
        texto_normalizado = normalizar_identificador(texto)
        coincidencia = PATRON_PATENTE.search(texto_normalizado)
        if coincidencia:
            coincidencias.append((confianza, coincidencia.group(0)))

    if not coincidencias:
        return None
    return max(coincidencias, key=lambda resultado: resultado[0])


def obtener_estado_actual(patente, vehiculos, cambios_estado):
    identificador = normalizar_identificador(patente)
    cambio = cambios_estado.get(identificador)
    if cambio:
        return cambio

    coincidencias = vehiculos[
        vehiculos["patente"].map(normalizar_identificador) == identificador
    ]
    if coincidencias.empty:
        return None
    return str(coincidencias.iloc[0]["estado_operativo"]).strip()


def validar_acceso(patente, rut, datos, cambios_estado):
    patente_normalizada = normalizar_identificador(patente)
    rut_normalizado = normalizar_identificador(rut)
    vehiculos = datos["vehiculos"]
    conductores = datos["conductores"]
    autorizaciones = datos["autorizaciones"]

    vehiculo = vehiculos[
        vehiculos["patente"].map(normalizar_identificador) == patente_normalizada
    ]
    conductor = conductores[
        conductores["rut"].map(normalizar_identificador) == rut_normalizado
    ]
    if vehiculo.empty:
        return "Denegado", "La patente no está registrada."
    if conductor.empty:
        return "Denegado", "El conductor no está registrado."
    if conductor.iloc[0]["estado"] != "activo":
        return "Denegado", "El conductor no figura como activo."

    autorizacion = autorizaciones[
        (autorizaciones["patente"].map(normalizar_identificador) == patente_normalizada)
        & (autorizaciones["rut_conductor"].map(normalizar_identificador) == rut_normalizado)
        & autorizaciones["vigente"]
    ]
    if autorizacion.empty:
        return "Denegado", "No hay una autorización vigente para esta pareja de vehículo y conductor."

    estado = obtener_estado_actual(patente, vehiculos, cambios_estado)
    if estado != "Operativo":
        return "Alerta de estado", f"El vehículo figura como «{estado}» y no debe ingresar."

    return "Autorizado", "El vehículo, el conductor y la autorización están vigentes."


def mostrar_guardia(datos):
    st.header("Control de acceso")
    st.write(
        "Fotografía la patente o carga una imagen. El lector la reconoce y "
        "puedes corregirla antes de verificar el acceso."
    )
    conductores_activos = datos["conductores"].loc[
        datos["conductores"]["estado"] == "activo"
    ]
    if conductores_activos.empty:
        st.error("No hay conductores activos en conductores.csv.")
        return

    opciones_conductores = crear_opciones_conductores(conductores_activos)

    fuente_foto = st.segmented_control(
        "Cómo proporcionar la fotografía",
        options=["Tomar fotografía", "Subir imagen"],
        default="Tomar fotografía",
        key="fuente_foto_patente",
    )
    foto = None
    if fuente_foto == "Tomar fotografía":
        foto = st.camera_input(
            "Fotografiar la patente del vehículo",
            key="foto_patente_camara",
            resolution="480p",
        )
    elif fuente_foto == "Subir imagen":
        foto = st.file_uploader(
            "Seleccionar una fotografía de la patente",
            type=["jpg", "jpeg", "png"],
            max_upload_size=1,
            key="foto_patente_archivo",
            help="La evidencia debe ocupar como máximo 800 KB.",
        )

    contenido_foto = foto.getvalue() if foto is not None else None
    foto_valida = True
    hash_foto = sha256(contenido_foto).hexdigest() if contenido_foto else None
    st.session_state.setdefault("hash_foto_patente_procesada", None)
    st.session_state.setdefault("patente_control_acceso", "")

    if contenido_foto:
        if len(contenido_foto) > TAMANO_MAXIMO_FOTO:
            st.error("La fotografía supera el límite de 800 KB. Reduce su tamaño y vuelve a cargarla.")
            foto_valida = False
        else:
            if hash_foto != st.session_state["hash_foto_patente_procesada"]:
                try:
                    with st.spinner(
                        "Preparando el lector y leyendo la patente. "
                        "La primera lectura puede tardar más..."
                    ):
                        lectura = leer_texto_patente(contenido_foto)
                except (UnidentifiedImageError, OSError, RuntimeError) as error:
                    st.error(f"No se pudo leer la fotografía: {error}")
                    foto_valida = False
                else:
                    patente_detectada = extraer_patente_detectada(lectura)
                    st.session_state["hash_foto_patente_procesada"] = hash_foto
                    st.session_state["patente_control_acceso"] = (
                        patente_detectada[1] if patente_detectada else ""
                    )
                    if patente_detectada:
                        st.success(
                            f"Patente reconocida: **{patente_detectada[1]}** "
                            f"({patente_detectada[0]:.0%} de confianza). "
                            "Confirma o corrige la lectura."
                        )
                    else:
                        st.warning(
                            "No se reconoció una patente con el formato esperado. "
                            "Escríbela manualmente."
                        )
            if foto_valida:
                st.image(contenido_foto, caption="Fotografía de la patente")

    with st.form("formulario_control_acceso"):
        patente = st.text_input(
            "Patente del vehículo",
            placeholder="Ej.: ABCD-12",
            key="patente_control_acceso",
        )
        nombre_conductor = st.selectbox("Conductor", list(opciones_conductores))
        enviado = st.form_submit_button("Verificar acceso", type="primary")

    if not enviado:
        return
    if not foto_valida:
        st.error("Corrige la fotografía antes de verificar el acceso.")
        return
    if not patente.strip():
        st.error("Fotografía la patente o ingrésala manualmente.")
        return

    resultado, motivo = validar_acceso(
        patente,
        opciones_conductores[nombre_conductor],
        datos,
        st.session_state["cambios_estado"],
    )
    try:
        _, url_fotografia = subir_fotografia(
            BUCKET_ACCESOS,
            "accesos",
            contenido_foto,
        )
    except (HTTPError, RuntimeError, StorageException, ValueError) as error:
        st.error(f"No se pudo guardar la fotografía del acceso en Supabase Storage: {error}")
        return

    st.session_state["accesos"].insert(
        0,
        {
            "fecha": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "patente": patente.strip().upper(),
            "conductor": nombre_conductor,
            "resultado": resultado,
            "motivo": motivo,
            "foto_evidencia_url": url_fotografia,
            "nombre_fotografia": foto.name if foto is not None else None,
        },
    )
    if resultado == "Autorizado":
        st.success(f"{resultado}: {motivo}")
    elif resultado == "Denegado":
        st.error(f"{resultado}: {motivo}")
    else:
        st.warning(f"{resultado}: {motivo}")


def mostrar_conductor(datos):
    st.header("Inspección del vehículo")
    st.write("Registra el estado de la unidad y sus novedades.")

    conductores_activos = datos["conductores"].loc[
        datos["conductores"]["estado"] == "activo"
    ]
    if conductores_activos.empty:
        st.error("No hay conductores activos en conductores.csv.")
        return

    opciones_conductores = crear_opciones_conductores(conductores_activos)
    with st.form("formulario_inspeccion"):
        nombre_conductor = st.selectbox("Conductor", list(opciones_conductores))
        rut = opciones_conductores[nombre_conductor]
        patentes_asignadas = datos["autorizaciones"].loc[
            (datos["autorizaciones"]["rut_conductor"] == rut)
            & datos["autorizaciones"]["vigente"],
            "patente",
        ].drop_duplicates()
        if patentes_asignadas.empty:
            st.info("Este conductor no tiene vehículos con autorización vigente.")
            st.form_submit_button("Enviar inspección", disabled=True)
            return

        patente = st.selectbox("Vehículo asignado", patentes_asignadas.tolist())
        kilometraje = st.number_input(
            "Kilometraje actual",
            min_value=0,
            step=100,
            value=int(
                datos["vehiculos"].loc[
                    datos["vehiculos"]["patente"] == patente, "kilometraje_actual"
                ].iloc[0]
            ),
        )
        combustible = st.slider("Combustible", min_value=0, max_value=100, value=50, format="%d%%")
        neumaticos = st.selectbox(
            "Neumáticos",
            ("En buen estado", "Requieren revisión", "Falla crítica"),
        )
        luces = st.selectbox("Luces", ("En buen estado", "Con fallas"))
        frenos = st.selectbox("Frenos", ("En buen estado", "Con fallas"))
        novedades = st.text_area("Novedades mecánicas")
        fotografia = st.file_uploader(
            "Fotografía de evidencia (opcional, máximo 800 KB)",
            type=["jpg", "jpeg", "png"],
            max_upload_size=1,
        )
        enviado = st.form_submit_button("Enviar inspección", type="primary")

    if not enviado:
        return
    if fotografia is not None and fotografia.size > TAMANO_MAXIMO_FOTO:
        st.error("La fotografía supera el límite de 800 KB. Reduce su tamaño y vuelve a adjuntarla.")
        return

    hallazgo_critico = neumaticos == "Falla crítica" or frenos == "Con fallas"
    requiere_revision = (
        hallazgo_critico
        or neumaticos == "Requieren revisión"
        or luces == "Con fallas"
        or bool(novedades.strip())
    )
    if hallazgo_critico:
        estado = "Mantenimiento urgente"
    elif requiere_revision:
        estado = "Requiere Mantención"
    else:
        estado = "Operativo"

    reporte = {
        "fecha": datetime.now().astimezone().isoformat(timespec="seconds"),
        "patente": patente,
        "conductor": nombre_conductor,
        "rut_conductor": rut,
        "kilometraje": int(kilometraje),
        "combustible": int(combustible),
        "neumaticos": neumaticos,
        "luces": luces,
        "frenos": frenos,
        "novedades": novedades.strip() or "Sin novedades.",
        "estado": estado,
        "fotografia": fotografia.getvalue() if fotografia is not None else None,
        "nombre_fotografia": fotografia.name if fotografia is not None else None,
    }
    objeto_fotografia = ""
    try:
        objeto_fotografia, url_fotografia = subir_fotografia(
            BUCKET_REPORTES,
            "reportes",
            reporte["fotografia"],
        )
        reporte_guardado = guardar_reporte_local(reporte, url_fotografia)
    except (
        HTTPError,
        OSError,
        RuntimeError,
        StorageException,
        ValueError,
        pd.errors.ParserError,
    ) as error:
        if objeto_fotografia:
            try:
                eliminar_fotografia(BUCKET_REPORTES, objeto_fotografia)
            except (HTTPError, StorageException) as error_limpieza:
                st.warning(
                    "No se pudo eliminar de Supabase Storage la fotografía de una "
                    f"inspección que no se guardó: {error_limpieza}"
                )
        st.error(f"No se pudo guardar la inspección y su fotografía: {error}")
        return

    reporte["foto_evidencia_url"] = reporte_guardado["foto_evidencia_url"]
    reporte.pop("fotografia")
    st.session_state["reportes"].insert(0, reporte)
    st.session_state["cambios_estado"][normalizar_identificador(patente)] = estado

    if estado == "Mantenimiento urgente":
        st.error("Inspección guardada en reportes_estado.csv. El vehículo requiere mantenimiento urgente.")
    elif estado == "Requiere Mantención":
        st.warning("Inspección guardada en reportes_estado.csv. La unidad requiere revisión.")
    else:
        st.success("Inspección guardada en reportes_estado.csv. La unidad figura operativa.")


def mostrar_puesto_de_mando(datos):
    st.header("Estado de la flota")
    reportes_guardados = cargar_reportes_guardados()
    cambios_estado = dict(st.session_state["cambios_estado"])
    if not reportes_guardados.empty and "estado_operativo" in reportes_guardados:
        reportes_ordenados = reportes_guardados.copy()
        reportes_ordenados["_fecha_orden"] = pd.to_datetime(
            reportes_ordenados.get(
                "fecha_reporte",
                pd.Series(index=reportes_ordenados.index, dtype="object"),
            ),
            errors="coerce",
            utc=True,
        )
        reportes_ordenados = reportes_ordenados.sort_values(
            "_fecha_orden", na_position="first"
        )
        for _, fila in reportes_ordenados.iterrows():
            estado_reportado = fila.get("estado_operativo")
            patente_reportada = fila.get("patente")
            if (
                pd.notna(estado_reportado)
                and pd.notna(patente_reportada)
                and str(estado_reportado).strip()
            ):
                cambios_estado[normalizar_identificador(patente_reportada)] = str(
                    estado_reportado
                ).strip()

    vehiculos = datos["vehiculos"].copy()
    vehiculos["estado_actual"] = vehiculos["patente"].map(
        lambda patente: obtener_estado_actual(
            patente, datos["vehiculos"], cambios_estado
        )
    )
    total = len(vehiculos)
    operativos = int((vehiculos["estado_actual"] == "Operativo").sum())
    mantenimiento = int((vehiculos["estado_actual"] == "Requiere Mantención").sum())
    urgentes = int(
        vehiculos["estado_actual"].isin(["Crítico", "Mantenimiento urgente"]).sum()
    )
    disponibilidad = operativos / total * 100 if total else 0

    kpi_disponibilidad, kpi_urgentes, kpi_revision = st.columns(3)
    kpi_disponibilidad.metric("Disponibilidad de la flota", f"{disponibilidad:.1f}%")
    kpi_urgentes.metric("Inmovilizados o urgentes", urgentes)
    kpi_revision.metric("Requieren mantención", mantenimiento)

    st.subheader("Estado por vehículo")
    vista_flota = vehiculos[
        ["patente", "tipo_vehiculo", "marca_modelo", "kilometraje_actual", "estado_actual"]
    ].rename(
        columns={
            "patente": "Patente",
            "tipo_vehiculo": "Tipo",
            "marca_modelo": "Marca y modelo",
            "kilometraje_actual": "Kilometraje",
            "estado_actual": "Estado",
        }
    )
    vista_flota.insert(
        0,
        "Semáforo",
        vista_flota["Estado"].map(ICONOS_ESTADO).fillna("⚪"),
    )
    st.dataframe(vista_flota, hide_index=True)

    st.subheader("Kilometraje por vehículo")
    kilometraje_por_vehiculo = vehiculos.set_index("patente")["kilometraje_actual"]
    st.bar_chart(kilometraje_por_vehiculo, x_label="Patente", y_label="Kilómetros")

    st.subheader("Accesos de esta sesión")
    if st.session_state["accesos"]:
        accesos = pd.DataFrame(
            [
                {
                    clave: valor
                    for clave, valor in acceso.items()
                    if clave not in {"foto_evidencia_url", "nombre_fotografia"}
                }
                for acceso in st.session_state["accesos"]
            ]
        )
        st.dataframe(accesos, hide_index=True)
        st.caption("Verificaciones por vehículo durante esta sesión")
        st.bar_chart(accesos.groupby("patente").size(), x_label="Patente", y_label="Verificaciones")
        for acceso in st.session_state["accesos"]:
            with st.expander(
                f"{acceso['patente']} — {acceso['fecha']} — {acceso['resultado']}"
            ):
                st.write(f"Conductor: {acceso['conductor']}")
                st.write(f"Resultado: {acceso['motivo']}")
                if acceso["foto_evidencia_url"]:
                    st.image(
                        acceso["foto_evidencia_url"],
                        caption=acceso["nombre_fotografia"] or "Evidencia de patente",
                    )
    else:
        st.info("Todavía no se han realizado verificaciones de acceso en esta sesión.")

    st.subheader("Inspecciones guardadas")
    st.caption(f"Archivo local: {ARCHIVO_REPORTES.name}")
    if reportes_guardados.empty:
        st.info("Todavía no se han guardado inspecciones.")
        return

    columnas_disponibles = [
        columna
        for columna in (
            "fecha_reporte",
            "patente",
            "rut_conductor",
            "kilometraje_salida",
            "nivel_combustible",
            "presion_neumaticos",
            "luces_operativas",
            "frenos_operativos",
            "estado_operativo",
            "observaciones",
        )
        if columna in reportes_guardados.columns
    ]
    st.dataframe(
        reportes_guardados[columnas_disponibles],
        hide_index=True,
    )
    for _, reporte in reportes_guardados.iloc[::-1].iterrows():
        patente_reporte = reporte.get("patente", "Sin patente")
        fecha_reporte = reporte.get("fecha_reporte", "Sin fecha")
        estado_reporte = reporte.get("estado_operativo", "Sin estado")
        with st.expander(f"{patente_reporte} — {fecha_reporte} — {estado_reporte}"):
            st.write(f"Conductor (RUT): {reporte.get('rut_conductor', 'Sin dato')}")
            st.write(f"Kilometraje: {reporte.get('kilometraje_salida', 'Sin dato')} km")
            st.write(f"Combustible: {reporte.get('nivel_combustible', 'Sin dato')}%")
            st.write(f"Neumáticos: {reporte.get('presion_neumaticos', 'Sin dato')}")
            st.write(f"Luces: {reporte.get('luces_operativas', 'Sin dato')}")
            st.write(f"Frenos: {reporte.get('frenos_operativos', 'Sin dato')}")
            st.write(f"Observaciones: {reporte.get('observaciones', 'Sin dato')}")
            url_fotografia = reporte.get("foto_evidencia_url", "")
            if isinstance(url_fotografia, str) and url_fotografia.startswith("https://"):
                st.image(url_fotografia, caption="Evidencia guardada en Supabase Storage")


def main():
    st.set_page_config(
        page_title="Control vehicular",
        page_icon=":material/local_shipping:",
        layout="wide",
    )
    st.title("Control de acceso y apresto vehicular")
    st.session_state.setdefault("accesos", [])
    st.session_state.setdefault("reportes", [])
    st.session_state.setdefault("cambios_estado", {})

    perfil = st.sidebar.selectbox("Perfil", PERFILES, key="perfil_demo")
    st.sidebar.caption("Selecciona el perfil que usarás en esta sesión.")

    try:
        datos = cargar_datos()
    except (FileNotFoundError, ValueError, pd.errors.ParserError) as error:
        st.error(f"No se pudieron cargar los datos de demostración: {error}")
        st.stop()

    if perfil == "Guardia de garita":
        mostrar_guardia(datos)
    elif perfil == "Conductor operativo":
        mostrar_conductor(datos)
    else:
        mostrar_puesto_de_mando(datos)


if __name__ == "__main__":
    main()

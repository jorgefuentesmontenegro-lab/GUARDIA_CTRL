# Control de acceso y apresto vehicular

Aplicación Streamlit para consultar accesos, inspeccionar vehículos y revisar el
estado de la flota. La aplicación reconoce patentes con EasyOCR y guarda las
fotografías en los buckets de Supabase Storage `evidencias-garita` y
`evidencias-vehiculares`. Los reportes se guardan en `reportes_estado.csv` en el
equipo donde se ejecuta la aplicación.

## Requisitos

- Python 3.11 o posterior.
- Los buckets de Supabase `evidencias-garita` y `evidencias-vehiculares`.
- Una URL y una clave de Supabase configuradas localmente.

## Ejecución local

Desde la carpeta del proyecto, en PowerShell:

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
```

Edita `.streamlit/secrets.toml` con la URL y la clave de Supabase del entorno.
Luego inicia la aplicación:

```powershell
streamlit run App.py
```

La aplicación usa `vehiculos.csv`, `conductores.csv` y `autorizaciones.csv`
locales cuando están presentes. En una copia limpia del repositorio usa los
datos ficticios de `datos_demo/`.

## Exportar datos desde Supabase

El programa `Importador_datos.py` necesita las variables de entorno
`SUPABASE_URL` y `SUPABASE_KEY`. No escribas las claves en el código ni las
subas al repositorio. Genera archivos CSV y XLSX en la carpeta actual.

## Datos y secretos

- No subas `.streamlit/secrets.toml`, claves, informes reales ni los CSV locales
  con información personal.
- `.gitignore` excluye secretos, el entorno virtual y los archivos de datos
  locales.
- `reportes_estado.csv` se conserva en el equipo que ejecuta la aplicación; no
  es una base de datos compartida ni persistente al desplegar en servicios con
  almacenamiento efímero.
- La selección de perfil es solo navegación de demostración, no autenticación.
- No despliegues la aplicación públicamente con una clave secreta de Supabase
  mientras no haya autenticación y políticas de acceso apropiadas.

## Esquema SQL opcional

`database/auth_schema.sql` prepara una tabla de perfiles para una futura etapa
de autenticación. La aplicación actual todavía no la utiliza.

import os

import pandas as pd
from supabase import create_client


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit(
        "Configura las variables de entorno SUPABASE_URL y SUPABASE_KEY."
    )

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
tablas = ["conductores", "vehiculos", "autorizaciones", "reportes_estado"]

for tabla in tablas:
    respuesta = supabase.table(tabla).select("*").execute()
    datos = respuesta.data
    df = pd.DataFrame(datos)
    df.to_csv(f"{tabla}.csv", index=False, encoding="utf-8-sig")
    df.to_excel(f"{tabla}.xlsx", index=False)
    if datos:
        print(f"Tabla {tabla} descargada correctamente.")
    else:
        print(f"La tabla {tabla} está vacía.")

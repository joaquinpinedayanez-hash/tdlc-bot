import os
import re
import hashlib
import json
import requests
import datetime as dt
import pdfplumber
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

HASH_FILE      = "last_hash.txt"
PAGE_HASH_FILE = "last_page_hash.txt"
URL_BASE       = "https://consultas.tdlc.cl"

# ── Fecha de hoy en Chile (UTC-4) ─────────────────────────────────────────────
hoy_chile = dt.datetime.now() - dt.timedelta(hours=4)
HOY_MS    = int(
    dt.datetime(hoy_chile.year, hoy_chile.month, hoy_chile.day,
                tzinfo=dt.timezone.utc).timestamp() * 1000
)


# ── Helpers de navegación con reintentos ──────────────────────────────────────
def goto_con_reintentos(page, url, intentos=3, espera_extra=5000, **kwargs):
    """Navega con reintentos y wait_until más tolerante (domcontentloaded en vez de networkidle)."""
    kwargs.setdefault("wait_until", "domcontentloaded")
    kwargs.setdefault("timeout", 60000)
    for intento in range(1, intentos + 1):
        try:
            page.goto(url, **kwargs)
            page.wait_for_timeout(espera_extra)
            return
        except PlaywrightTimeoutError as e:
            print(f"  ⚠️ Timeout navegando a {url} (intento {intento}/{intentos}): {e}")
            if intento == intentos:
                raise
            page.wait_for_timeout(3000)


def reload_con_reintentos(page, intentos=3, espera_extra=5000, **kwargs):
    """Recarga con reintentos y wait_until más tolerante."""
    kwargs.setdefault("wait_until", "domcontentloaded")
    kwargs.setdefault("timeout", 30000)
    for intento in range(1, intentos + 1):
        try:
            page.reload(**kwargs)
            page.wait_for_timeout(espera_extra)
            return
        except PlaywrightTimeoutError as e:
            print(f"  ⚠️ Timeout recargando (intento {intento}/{intentos}): {e}")
            if intento == intentos:
                raise
            page.wait_for_timeout(3000)


def esperar_icono_con_reintentos(page, url_recarga, intentos=3, timeout=15000):
    """
    Espera el ícono '.glyphicon-new-window' con reintentos.
    Si no aparece, vuelve a navegar a url_recarga (con su propio
    goto_con_reintentos) antes de reintentar, en vez de rendirse
    tras un único intento de 15s como antes.
    """
    for intento in range(1, intentos + 1):
        try:
            return page.wait_for_selector(".glyphicon-new-window", timeout=timeout)
        except PlaywrightTimeoutError:
            print(f"  ⚠️ Ícono no apareció (intento {intento}/{intentos})")
            if intento == intentos:
                return None
            page.wait_for_timeout(3000)
            goto_con_reintentos(page, url_recarga)
    return None


# ── Hash de página ────────────────────────────────────────────────────────────
def load_page_hash():
    if os.path.exists(PAGE_HASH_FILE):
        with open(PAGE_HASH_FILE) as f:
            return f.read().strip()
    return ""

def save_page_hash(h):
    with open(PAGE_HASH_FILE, "w") as f:
        f.write(h)

def push_page_hash():
    try:
        subprocess.run(["git", "config", "user.name",  "tdlc-bot"],  check=True)
        subprocess.run(["git", "config", "user.email", "bot@tdlc"],  check=True)
        subprocess.run(["git", "add", PAGE_HASH_FILE],               check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "update page hash"], check=True)
            subprocess.run(["git", "push", "origin", "main"],           check=True)
        print("✅ Hash de página guardado en repo")
    except Exception as e:
        print(f"⚠️  No se pudo guardar hash de página: {e}")


# ── Descarga PDF con cookies de Playwright ────────────────────────────────────
def descargar_pdf(cookies_dict, url_pdf):
    session = requests.Session()
    for nombre, valor in cookies_dict.items():
        session.cookies.set(nombre, valor)
    session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": URL_BASE})
    try:
        resp = session.get(url_pdf, timeout=30)
        if resp.status_code == 200 and resp.content[:4] == b'%PDF':
            with open("temp_resolucion.pdf", "wb") as f:
                f.write(resp.content)
            texto = ""
            with pdfplumber.open("temp_resolucion.pdf") as pdf:
                for p in pdf.pages:
                    t = p.extract_text()
                    if t:
                        texto += t + "\n"
            return texto.strip()
        return None
    except Exception as e:
        print(f"  Error descargando PDF: {e}")
        return None


# ── Limpieza de texto extraído del PDF ───────────────────────────────────────
def limpiar_contenido(texto):
    """Elimina encabezados de página y bloque de firma electrónica del PDF."""
    texto = re.sub(
        r'\n\d+\s*\n[^\n]*(?:REP[ÚU\s]{0,2}BLICA|REPÚBLICA|REPUBLICA)\s+DE\s+CHILE[^\n]*\n'
        r'TRIBUNAL DE DEFENSA DE LA LIBRE COMPETENCIA\n',
        '\n',
        texto
    )
    texto = re.sub(
        r'\s*Autorizada por la Secretaria Abogada\(S\),.*?'
        r'verificación indicado bajo el código de barras\.',
        '',
        texto,
        flags=re.DOTALL
    )
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


# ── Helpers de cuadernos (portados desde Colab/Selenium) ─────────────────────

def obtener_nombres_cuadernos(page):
    """
    Lee las opciones del dropdown de cuadernos directamente del DOM.
    Retorna lista de dicts {index, text}.
    """
    try:
        opciones = page.evaluate("""() => {
            const s = document.querySelector('select[name="selectCuaderno"]');
            if (!s) return [];
            return Array.from(s.options).map((o, i) => ({
                index: i,
                text:  o.text.trim()
            }));
        }""")
        return opciones if opciones else []
    except Exception as e:
        print(f"  Error leyendo cuadernos del DOM: {e}")
        return []


def seleccionar_cuaderno_y_capturar_id(page, indice, nombre):
    """
    Selecciona el cuaderno por índice en el dropdown,
    espera la petición de red y retorna el idCuaderno capturado.
    """
    try:
        id_capturado = None

        def capturar_request(request):
            nonlocal id_capturado
            if "bloqueadossummary" in request.url and id_capturado is None:
                partes = request.url.split("/")
                if "bloqueadossummary" in partes:
                    idx = partes.index("bloqueadossummary")
                    id_capturado = partes[idx + 1]

        page.on("request", capturar_request)

        # Seleccionar la opción por índice mediante JS para evitar problemas
        # con selects que no responden al método nativo de Playwright
        page.evaluate(f"""() => {{
            const s = document.querySelector('select[name="selectCuaderno"]');
            if (s) {{
                s.selectedIndex = {indice};
                s.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        }}""")
        print(f"    ✔ Seleccionado índice {indice} ('{nombre}') en dropdown")

        # Esperar hasta 10 s a que llegue la request, chequeando cada 500 ms
        for _ in range(20):
            if id_capturado:
                break
            page.wait_for_timeout(500)

        page.remove_listener("request", capturar_request)
        return id_capturado

    except Exception as e:
        print(f"    ❌ Error seleccionando cuaderno '{nombre}': {e}")
        return None


def procesar_tramites_cuaderno(page, cookies_dict, id_cuaderno, nombre_cuaderno):
    """
    Dado un idCuaderno, obtiene sus trámites y retorna las resoluciones
    de hoy con el texto del PDF extraído.
    """
    resp_raw = page.evaluate(f"""() => {{
        var xhr = new XMLHttpRequest();
        xhr.open('GET',
            '{URL_BASE}/rest/tramite/bloqueadossummary/{id_cuaderno}/10000/1/true/false',
            false);
        xhr.setRequestHeader('Accept', 'application/json');
        xhr.send();
        return xhr.responseText;
    }}""")

    try:
        data     = json.loads(resp_raw)
        tramites = data.get("results", data) if isinstance(data, dict) else data
    except Exception:
        print(f"    ❌ Error JSON en cuaderno '{nombre_cuaderno}'")
        return []

    resoluciones_hoy = [
        t for t in tramites
        if isinstance(t, dict)
        and t.get("tipoTramite") == "Resolución"
        and t.get("fecha", 0) >= HOY_MS
    ]
    print(f"    Resoluciones de hoy en '{nombre_cuaderno}': {len(resoluciones_hoy)}")

    encontradas = []
    for tramite in resoluciones_hoy:
        id_enc     = tramite.get("idDocumentoEncriptado")
        referencia = tramite.get("referencia", "sin referencia")
        fecha_dt   = dt.datetime.fromtimestamp(tramite.get("fecha", 0) / 1000)
        print(f"    📄 {referencia} | {fecha_dt}")

        if not id_enc:
            continue

        # Intentar dos variantes de URL
        for url_pdf in [
            f"{URL_BASE}/download/{id_enc}?inlineifpossible=true",
            f"{URL_BASE}/download/{id_enc}",
        ]:
            texto = descargar_pdf(cookies_dict, url_pdf)
            if texto:
                print(f"    ✅ PDF extraído ({len(texto)} chars)")
                encontradas.append({
                    "cuaderno":   nombre_cuaderno,
                    "referencia": referencia,
                    "fecha":      fecha_dt.strftime("%d/%m/%Y %H:%M"),
                    "contenido":  texto,
                })
                break
        else:
            print(f"    ⚠️  No se pudo descargar PDF de '{referencia}'")

    return encontradas


# ── Scraping principal ────────────────────────────────────────────────────────
def fetch_tdlc():
    resultados  = []
    causas_hash = None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page    = context.new_page()

        # ── Paso 1: abrir modal y leer lista de causas ────────────────────
        goto_con_reintentos(page, f"{URL_BASE}/estadoDiario")

        detalle_icon = esperar_icono_con_reintentos(page, f"{URL_BASE}/estadoDiario")
        if not detalle_icon:
            print("Error abriendo modal: ícono no apareció tras varios reintentos")
            browser.close()
            return [], None
        detalle_icon.click()
        page.wait_for_timeout(4000)
        print("✅ Modal abierto")

        filas  = page.query_selector_all("#showDetalle tbody tr")
        causas = []
        for fila in filas:
            celdas = fila.query_selector_all("td")
            if len(celdas) >= 2:
                causas.append({
                    "rol":      celdas[0].inner_text().strip(),
                    "caratula": celdas[1].inner_text().strip(),
                })
        print(f"✅ {len(causas)} causas en el estado diario de hoy")

        # ── Paso 2: calcular hash de la lista y comparar ──────────────────
        causas_hash = hashlib.md5(
            json.dumps(causas, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

        if causas_hash == load_page_hash():
            print("📄 Lista de causas sin cambios. Nada que hacer.")
            browser.close()
            return [], causas_hash

        save_page_hash(causas_hash)
        push_page_hash()
        print("🔄 Lista de causas cambió, entrando a cada causa...")

        # ── Paso 3: scraping completo ─────────────────────────────────────
        for i, causa in enumerate(causas):
            print(f"\n📂 [{i+1}/{len(causas)}] {causa['rol']} — {causa['caratula']}")

            # Reabrir modal para obtener el span de esta causa
            goto_con_reintentos(page, f"{URL_BASE}/estadoDiario")
            icono = esperar_icono_con_reintentos(page, f"{URL_BASE}/estadoDiario")
            if not icono:
                print(f"  ⚠️ No cargó el ícono para {causa['rol']} tras varios reintentos, se omite")
                continue
            iconos = page.query_selector_all(".glyphicon-new-window")
            if not iconos:
                print(f"  ⚠️ Sin íconos para {causa['rol']}, se omite")
                continue
            iconos[0].click()
            page.wait_for_timeout(4000)

            spans_causa = page.query_selector_all(
                "#showDetalle tbody tr td span.glyphicon-new-window"
            )
            if i >= len(spans_causa):
                continue

            # Abrir página de la causa en nueva pestaña
            with context.expect_page() as nueva_page_info:
                spans_causa[i].click()
            nueva_page = nueva_page_info.value
            try:
                nueva_page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                print("  ⚠️ Timeout esperando carga de nueva página, continuando de todos modos")
            nueva_page.wait_for_timeout(8000)

            url_causa = nueva_page.url
            id_causa  = (
                url_causa.split("idCausa=")[-1].split("&")[0]
                if "idCausa=" in url_causa else None
            )
            print(f"  idCausa: {id_causa}")

            # ── Capturar idCuaderno principal escuchando requests ─────────
            id_cuaderno_principal = None
            requests_capturados   = []

            def capturar_request(request):
                if "bloqueadossummary" in request.url:
                    requests_capturados.append(request.url)

            nueva_page.on("request", capturar_request)
            reload_con_reintentos(nueva_page, espera_extra=10000)

            for url_req in requests_capturados:
                partes = url_req.split("/")
                if "bloqueadossummary" in partes:
                    idx                   = partes.index("bloqueadossummary")
                    id_cuaderno_principal = partes[idx + 1]
                    break

            nueva_page.remove_listener("request", capturar_request)
            print(f"  idCuaderno principal: {id_cuaderno_principal}")

            if not id_cuaderno_principal:
                nueva_page.close()
                continue

            cookies_dict = {c["name"]: c["value"] for c in context.cookies()}

            # ── Cuaderno principal ────────────────────────────────────────
            for r in procesar_tramites_cuaderno(
                nueva_page, cookies_dict, id_cuaderno_principal, "Cuaderno principal"
            ):
                resultados.append({**causa, **r})

            # ── Cuadernos adicionales ─────────────────────────────────────
            cuadernos_dom   = obtener_nombres_cuadernos(nueva_page)
            cuadernos_extra = [c for c in cuadernos_dom if c["index"] != 0]
            print(f"  Cuadernos adicionales detectados: {len(cuadernos_extra)}")

            for cuaderno in cuadernos_extra:
                nombre_c = cuaderno["text"]
                idx_c    = cuaderno["index"]
                print(f"\n  📁 Procesando: '{nombre_c}' (índice={idx_c})")

                id_c = seleccionar_cuaderno_y_capturar_id(nueva_page, idx_c, nombre_c)
                print(f"    idCuaderno capturado: {id_c}")

                if not id_c:
                    # Fallback: recargar la página del expediente y reintentar
                    print(f"    ⚠️  Reintentando tras recarga...")
                    goto_con_reintentos(
                        nueva_page,
                        f"{URL_BASE}/estadoDiario?idCausa={id_causa}",
                        espera_extra=10000,
                        timeout=30000,
                    )
                    cookies_dict = {c["name"]: c["value"] for c in context.cookies()}
                    id_c = seleccionar_cuaderno_y_capturar_id(nueva_page, idx_c, nombre_c)
                    print(f"    idCuaderno reintento: {id_c}")

                if id_c:
                    for r in procesar_tramites_cuaderno(
                        nueva_page, cookies_dict, id_c, nombre_c
                    ):
                        resultados.append({**causa, **r})
                else:
                    print(f"    ❌ No se pudo obtener idCuaderno para '{nombre_c}', se omite")

            nueva_page.close()

        browser.close()

    return resultados, causas_hash


# ── Formatear mensaje (cuerpo del correo) ─────────────────────────────────────
def formatear_mensaje(resultados):
    hoy = hoy_chile.strftime("%d/%m/%Y")
    if not resultados:
        return f"📋 TDLC Estado Diario {hoy}\n\nNo se encontraron resoluciones nuevas hoy."

    msg  = f"📋 TDLC — Estado Diario {hoy}\n"
    msg += f"{'='*50}\n\n"
    msg += f"Se encontraron {len(resultados)} resolución(es):\n\n"

    for r in resultados:
        msg += f"{'-'*50}\n"
        msg += f"📁 {r['rol']}\n"
        msg += f"📌 {r['caratula']}\n"
        msg += f"🗂  {r.get('cuaderno', '')}\n"
        msg += f"⚖️  {r['referencia']}\n"
        msg += f"🕐 {r['fecha']}\n\n"

    msg += f"🔗 {URL_BASE}/estadoDiario"
    return msg


# ── Correo electrónico con adjunto TXT ───────────────────────────────────────
def send_email(message, resultados):
    import smtplib
    from email.mime.text     import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base     import MIMEBase
    from email               import encoders

    destinatarios = os.environ["EMAIL_TO"].split(",")
    hoy           = hoy_chile.strftime("%d/%m/%Y")
    ancho         = 70
    linea_gruesa  = "═" * ancho
    linea_delgada = "─" * ancho

    msg            = MIMEMultipart()
    msg["From"]    = os.environ["EMAIL_FROM"]
    msg["To"]      = ", ".join(destinatarios)
    msg["Subject"] = f"TDLC Estado Diario {hoy} — {len(resultados)} resolución(es)"
    msg.attach(MIMEText(message, "plain", "utf-8"))

    if resultados:
        txt  = f"{linea_gruesa}\n"
        txt += f" ESTADO DIARIO TDLC — {hoy}\n"
        txt += f" {len(resultados)} resolución(es)\n"
        txt += f"{linea_gruesa}\n\n"

        txt += "ÍNDICE\n"
        txt += f"{linea_delgada}\n"
        for i, r in enumerate(resultados, 1):
            txt += f"  {i:>2}. [{r['rol']}]  {r['referencia']}\n"
            txt += f"      {r['caratula']}\n"
            if r.get("cuaderno"):
                txt += f"      Cuaderno: {r['cuaderno']}\n"
        txt += f"\n{linea_gruesa}\n\n"

        for i, r in enumerate(resultados, 1):
            contenido_limpio = limpiar_contenido(r["contenido"])

            txt += f"RESOLUCIÓN {i} DE {len(resultados)}\n"
            txt += f"{linea_delgada}\n"
            txt += f"  Causa:      {r['rol']}\n"
            txt += f"  Carátula:   {r['caratula']}\n"
            if r.get("cuaderno"):
                txt += f"  Cuaderno:   {r['cuaderno']}\n"
            txt += f"  Resolución: {r['referencia']}\n"
            txt += f"  Fecha:      {r['fecha']}\n"
            txt += f"{linea_delgada}\n\n"

            for linea in contenido_limpio.splitlines():
                txt += f"  {linea}\n" if linea.strip() else "\n"

            txt += f"\n{linea_gruesa}\n\n"

        adjunto = MIMEBase("application", "octet-stream")
        adjunto.set_payload(txt.encode("utf-8"))
        encoders.encode_base64(adjunto)
        adjunto.add_header(
            "Content-Disposition",
            f"attachment; filename=resoluciones_tdlc_{hoy_chile.strftime('%Y-%m-%d')}.txt",
        )
        msg.attach(adjunto)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_FROM"], os.environ["GMAIL_PASSWORD"])
        server.sendmail(os.environ["EMAIL_FROM"], destinatarios, msg.as_string())
    print(f"Email enviado a {len(destinatarios)} destinatario(s)")


# ── Hash de resultados ────────────────────────────────────────────────────────
def get_hash(resultados):
    contenido = json.dumps(
        [{k: v for k, v in r.items() if k != "contenido"} for r in resultados],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.md5(contenido.encode()).hexdigest()

def load_last_hash():
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE) as f:
            return f.read().strip()
    return ""

def save_hash(h):
    with open(HASH_FILE, "w") as f:
        f.write(h)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Verificando TDLC — {hoy_chile.strftime('%d/%m/%Y %H:%M')}...")

    try:
        resultados, causas_hash = fetch_tdlc()
    except Exception as e:
        print(f"❌ Error fatal en fetch_tdlc: {e}")
        raise

    if not resultados:
        print("Sin resoluciones nuevas hoy.")
    else:
        current_hash = get_hash(resultados)
        if current_hash == load_last_hash():
            print("Sin cambios desde la última ejecución.")
        else:
            print(f"¡{len(resultados)} resolución(es) nueva(s)! Enviando notificación por correo...")
            mensaje = formatear_mensaje(resultados)
            send_email(mensaje, resultados)
            save_hash(current_hash)
            print("✅ Listo.")

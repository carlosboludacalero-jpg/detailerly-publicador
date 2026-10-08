"""Publica en Instagram lo que toca según cola.json. Lo ejecuta GitHub Actions cada 10 minutos.

Variables de entorno: IG_TOKEN, IG_USER_ID, BASE_URL (GitHub Pages de este repo), TG_TOKEN y TG_CHAT (avisos, opcionales).
Estado (lo ya publicado) en estado/publicado.json, que el workflow guarda en la rama «estado».
Uso local de prueba: python publicar.py --prueba  → crea los contenedores de la próxima publicación sin publicarla.
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://graph.facebook.com/"
TOKEN, IG = os.environ["IG_TOKEN"], os.environ["IG_USER_ID"]
BASE = os.environ["BASE_URL"].rstrip("/") + "/"
MARGEN = timedelta(hours=6)  # si GitHub se retrasa más que esto, no se publica tarde: se avisa
MAX_INTENTOS = 3
AQUI = os.path.dirname(os.path.abspath(__file__))
ESTADO = os.path.join(AQUI, "estado", "publicado.json")


class ErrorIG(Exception):
    pass


def llamar(metodo, ruta, **params):
    params["access_token"] = TOKEN
    datos = urllib.parse.urlencode(params).encode()
    if metodo == "GET":
        req = urllib.request.Request(API + ruta + "?" + datos.decode())
    else:
        req = urllib.request.Request(API + ruta, data=datos, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        err = json.load(e).get("error", {})
        raise ErrorIG(f"{err.get('message')} (código {err.get('code')}/{err.get('error_subcode')})") from None


def avisar(texto):
    tg, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    print(texto)
    if tg and chat:
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tg}/sendMessage",
                                   urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode(), timeout=20)
        except Exception as e:
            print("Telegram falló:", e)


def url(ruta):
    return BASE + urllib.parse.quote(ruta)


def esperar_listo(cid, minutos=10):
    for _ in range(minutos * 6):
        st = llamar("GET", cid, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise ErrorIG(f"Instagram no pudo procesar el archivo ({llamar('GET', cid, fields='status').get('status')})")
        time.sleep(10)
    raise ErrorIG("Instagram tardó más de 10 minutos en procesar el vídeo")


def contenedor(archivo, **extra):
    if archivo.endswith(".mp4"):
        cid = llamar("POST", f"{IG}/media", video_url=url(archivo), **extra)["id"]
    else:
        cid = llamar("POST", f"{IG}/media", image_url=url(archivo), **extra)["id"]
    esperar_listo(cid)
    return cid


def publicar(cid):
    return llamar("POST", f"{IG}/media_publish", creation_id=cid)["id"]


def crear(pub):
    """Devuelve una lista de (subclave, función que crea el contenedor) en el orden de publicación."""
    a, cap = pub["archivos"], pub["caption"]
    if pub["tipo"] == "reel":
        return [("", lambda: contenedor(a[0], media_type="REELS", caption=cap, share_to_feed="true"))]
    if pub["tipo"] == "foto":
        return [("", lambda: contenedor(a[0], caption=cap))]
    if pub["tipo"] == "carrusel":
        def carrusel():
            hijos = [contenedor(f, is_carousel_item="true", **({"media_type": "VIDEO"} if f.endswith(".mp4") else {})) for f in a[:10]]
            return contenedor_padre(hijos, cap)
        return [("", carrusel)]
    if pub["tipo"] == "historias":
        return [(f"#{k}", (lambda f=f: contenedor(f, media_type="STORIES"))) for k, f in enumerate(a)]
    raise ErrorIG(f"tipo desconocido {pub['tipo']}")


def contenedor_padre(hijos, cap):
    cid = llamar("POST", f"{IG}/media", media_type="CAROUSEL", children=",".join(hijos), caption=cap)["id"]
    esperar_listo(cid)
    return cid


def main():
    prueba = "--prueba" in sys.argv
    cola = json.load(open(os.path.join(AQUI, "cola.json"), encoding="utf-8"))
    estado = json.load(open(ESTADO, encoding="utf-8")) if os.path.exists(ESTADO) else {"hechos": {}, "intentos": {}}
    ahora = datetime.now(timezone.utc)

    if prueba:
        elegido = sys.argv[sys.argv.index("--prueba") + 1] if len(sys.argv) > sys.argv.index("--prueba") + 1 else None
        pub = next(p for p in cola if (p["id"] == elegido if elegido else
                                       p["id"] not in estado["hechos"] and datetime.fromisoformat(p["cuando"]) > ahora))
        print("Prueba con", pub["id"], pub["tipo"], len(pub["archivos"]), "archivos")
        for sub, fn in crear(pub):
            print("  contenedor listo:", sub or "principal", fn())
        print("OK: Instagram acepta los archivos (no se ha publicado nada)")
        return

    for pub in cola:
        pid, cuando = pub["id"], datetime.fromisoformat(pub["cuando"])
        if pid in estado["hechos"] or cuando > ahora:
            continue
        if ahora - cuando > MARGEN:
            estado["hechos"][pid] = "saltado"
            avisar(f"⚠️ No se publicó a tiempo: {pub['tipo']} del {pub['fecha']} {pub['hora']}. Súbelo a mano si quieres.")
            continue
        if estado["intentos"].get(pid, 0) >= MAX_INTENTOS:
            continue
        try:
            ids = []
            for sub, fn in crear(pub):
                if estado["hechos"].get(pid + sub):
                    continue
                ids.append(publicar(fn()))
                if sub:
                    estado["hechos"][pid + sub] = ids[-1]
            estado["hechos"][pid] = ids[-1] if ids else "ok"
            link = llamar("GET", ids[-1], fields="permalink").get("permalink", "") if ids else ""
            avisar(f"✅ Publicado en Instagram: {pub['tipo']} de las {pub['hora']}\n{pub['caption'][:80]}\n{link}".strip())
        except Exception as e:
            n = estado["intentos"][pid] = estado["intentos"].get(pid, 0) + 1
            avisar(f"❌ Falló {pub['tipo']} del {pub['fecha']} {pub['hora']} (intento {n}/{MAX_INTENTOS}): {e}")
        finally:
            os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
            json.dump(estado, open(ESTADO, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

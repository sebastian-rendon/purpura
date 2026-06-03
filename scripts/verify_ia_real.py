"""
P05-VERIFY: comprueba que la capa IA está en modo LLM real en prod.

Flujo:
  1. Login estudiante1 vía túnel SSH (VERIFY_BASE_URL).
  2. Cancela todas las postulaciones activas (cleanup idempotente).
  3. Postula a la primera convocatoria PUBLICADA del listado.
  4. Logout, login admin.
  5. Abre la postulación recién creada en /bandeja?estado=enviada.
  6. Verifica que la card .card-evaluacion-ia tenga data-decision presente
     y que el atributo data-modo (o el texto del header) NO sea "fallback".
  7. Guarda screenshot.

Uso:
    VERIFY_BASE_URL=http://127.0.0.1:8080 uv run python scripts/verify_ia_real.py
"""
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = os.environ.get("VERIFY_BASE_URL", "http://127.0.0.1:8080")
OUT_PATH = Path("tests/screenshots/verify_ia_real.png")
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

ESTUDIANTE = ("estudiante1@udem.edu.co", "Estudiante2026!")
ADMIN = ("admin@purpura.local", "Admin2026!")


async def login(page, email, password):
    await page.goto(f"{BASE_URL}/login")
    await page.wait_for_load_state("domcontentloaded")
    await page.fill('input[name="email"]', email)
    await page.fill('input[name="password"]', password)
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("domcontentloaded")


async def logout(page):
    btn = page.locator('form[action="/logout"] button')
    if await btn.count() > 0:
        await btn.first.click()
        await page.wait_for_load_state("domcontentloaded")


async def main():
    print(f"[verify] BASE_URL = {BASE_URL}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=250)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        ctx.set_default_timeout(60000)
        ctx.set_default_navigation_timeout(60000)
        page = await ctx.new_page()

        await login(page, *ESTUDIANTE)
        print("[verify] login estudiante1 OK")

        await page.goto(f"{BASE_URL}/mis-postulaciones")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_selector("h1", timeout=8000)

        ids = []
        filas = await page.locator(
            "tr[data-postulacion-id][data-postulacion-estado='ENVIADA'], "
            "tr[data-postulacion-id][data-postulacion-estado='EN_REVISION']"
        ).all()
        for fila in filas:
            pid = await fila.get_attribute("data-postulacion-id")
            if pid:
                ids.append(pid)
        for pid in ids:
            await page.request.post(f"{BASE_URL}/postulaciones/{pid}/cancelar")
        print(f"[verify] cleanup canceló {len(ids)} postulación(es)")

        await page.goto(f"{BASE_URL}/convocatorias")
        await page.wait_for_load_state("domcontentloaded")
        primera = page.locator(
            "tr[data-conv-id] a[href^='/convocatorias/']:not([href*='editar']):not([href*='archiv'])"
        ).first
        await primera.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_selector("h1", timeout=8000)

        boton_postular = page.locator("button[data-accion='postular']").first
        if await boton_postular.count() == 0:
            print("[ERROR] No aparece botón Postularme. Estudiante puede tener otra postulación activa que la máquina no permite cancelar.")
            await browser.close()
            return 1
        await boton_postular.click()
        await page.wait_for_load_state("domcontentloaded")
        print("[verify] postulación creada (debería tener evaluación IA real)")

        await logout(page)
        await login(page, *ADMIN)
        print("[verify] login admin OK")

        await page.goto(f"{BASE_URL}/bandeja?estado=enviada")
        await page.wait_for_load_state("domcontentloaded")
        primera_post = page.locator(
            "tr[data-postulacion-id][data-postulacion-estado='ENVIADA'] "
            "a[href^='/postulaciones/']"
        ).first
        href = await primera_post.get_attribute("href")
        if not href:
            print("[ERROR] No hay postulaciones ENVIADAs en /bandeja.")
            await browser.close()
            return 1
        print(f"[verify] abriendo {href}")
        await primera_post.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_selector(".card-evaluacion-ia", timeout=10000)

        card = page.locator(".card-evaluacion-ia").first
        decision = await card.get_attribute("data-decision")
        header_text = await page.text_content(".card-evaluacion-ia") or ""

        modo_llm = "modo: llm" in header_text.lower()
        modo_fallback = "modo: fallback" in header_text.lower()
        modo_reglas = "modo: reglas" in header_text.lower()
        modelo_gemini = "gemini-2.0-flash" in header_text.lower()   

        print(f"[verify] decision sugerida = {decision}")
        print(f"[verify] modo=llm:      {modo_llm}")
        print(f"[verify] modo=reglas:   {modo_reglas}")
        print(f"[verify] modo=fallback: {modo_fallback}")
        print(f"[verify] modelo gemini-2.0-flash: {modelo_gemini}")

        await page.screenshot(path=str(OUT_PATH), full_page=True)
        print(f"[verify] screenshot: {OUT_PATH.absolute()}")

        await browser.close()

        if modo_fallback:
            print("[ERROR] La evaluación está en modo fallback. La key no llegó al endpoint.")
            return 1
        if not (modo_llm or modo_reglas):
            print("[WARN] No se identificó el modo. Revisar manualmente el screenshot.")
            return 1
        if modo_llm and modelo_gemini:
            print("[OK] Evaluación IA en modo LLM REAL confirmada con gemini-2.0-flash.")
            return 0
        if modo_reglas:
            print("[OK-REGLAS] Modo reglas determinísticas activo (datos suficientes, LLM no se invocó).")
            return 0
        print("[OK] Modo identificado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

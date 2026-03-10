import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Permite llamadas desde el frontend de Jumpseller

# ─── CONFIGURACIÓN ───────────────────────────────────────────
JUMPSELLER_LOGIN    = os.environ.get('JUMPSELLER_LOGIN', '')
JUMPSELLER_TOKEN    = os.environ.get('JUMPSELLER_TOKEN', '')
BASE_URL            = 'https://api.jumpseller.com/v1'
PAGE_SIZE           = 100
MAX_WORKERS         = 10    # páginas en paralelo
CACHE_TTL_SECONDS   = 7200  # 2 horas

# ─── CACHÉ EN MEMORIA ─────────────────────────────────────────
catalog_cache = {
    'products': [],   # lista de productos con stock
    'index': {},      # dict: nombre_base → [productos]
    'loaded_at': 0,   # timestamp de última carga
    'loading': False  # flag para evitar cargas simultáneas
}
cache_lock = threading.Lock()


# ─── CARGA DEL CATÁLOGO ───────────────────────────────────────
def fetch_page(page):
    """Descarga una página de productos de Jumpseller."""
    r = requests.get(
        f'{BASE_URL}/products.json',
        params={
            'login': JUMPSELLER_LOGIN,
            'authtoken': JUMPSELLER_TOKEN,
            'limit': PAGE_SIZE,
            'page': page,
            'status': 'available'
        },
        timeout=15
    )
    r.raise_for_status()
    return r.json()


def build_index(products):
    """Construye índice por nombre base (antes del primer |)."""
    index = {}
    for p in products:
        base = p['name'].split('|')[0].strip().lower()
        if base not in index:
            index[base] = []
        index[base].append(p)
    return index


def load_catalog():
    """Carga todo el catálogo de Jumpseller con stock > 0."""
    with cache_lock:
        if catalog_cache['loading']:
            return
        catalog_cache['loading'] = True

    try:
        print('[Catalog] Iniciando carga...')

        # Total de productos
        r = requests.get(
            f'{BASE_URL}/products/count.json',
            params={'login': JUMPSELLER_LOGIN, 'authtoken': JUMPSELLER_TOKEN},
            timeout=10
        )
        total = r.json().get('count', 0)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        print(f'[Catalog] Total: {total} productos, {total_pages} páginas')

        # Descargar en lotes paralelos con ThreadPoolExecutor
        from concurrent.futures import ThreadPoolExecutor, as_completed

        all_products = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_page, p): p for p in range(1, total_pages + 1)}
            completed = 0
            for future in as_completed(futures):
                completed += 1
                try:
                    page_data = future.result()
                    if isinstance(page_data, list):
                        for item in page_data:
                            product = item.get('product', {})
                            if product.get('stock', 0) > 0:
                                # Guardar solo campos necesarios para reducir memoria
                                slim = {
                                    'id': product.get('id'),
                                    'name': product.get('name', ''),
                                    'price': product.get('price', 0),
                                    'stock': product.get('stock', 0),
                                    'status': product.get('status', ''),
                                    'images': product.get('images', [])[:1],  # solo primera imagen
                                    'fields': product.get('fields', []),
                                }
                                all_products.append(slim)
                except Exception as e:
                    print(f'[Catalog] Error en página: {e}')

                if completed % 20 == 0:
                    print(f'[Catalog] Progreso: {completed}/{total_pages} páginas')

        index = build_index(all_products)

        with cache_lock:
            catalog_cache['products'] = all_products
            catalog_cache['index'] = index
            catalog_cache['loaded_at'] = time.time()
            catalog_cache['loading'] = False

        print(f'[Catalog] Carga completa: {len(all_products)} productos con stock')

    except Exception as e:
        print(f'[Catalog] Error en carga: {e}')
        with cache_lock:
            catalog_cache['loading'] = False


def get_catalog():
    """Devuelve el catálogo, cargándolo si es necesario."""
    now = time.time()
    age = now - catalog_cache['loaded_at']

    if not catalog_cache['products'] or age > CACHE_TTL_SECONDS:
        if not catalog_cache['loading']:
            thread = threading.Thread(target=load_catalog, daemon=True)
            thread.start()
            # Si es la primera carga, esperar a que termine
            if not catalog_cache['products']:
                thread.join(timeout=60)

    return catalog_cache['index']


# ─── BÚSQUEDA ─────────────────────────────────────────────────
def search_in_index(name, index):
    """Busca una carta por nombre en el índice — solo match exacto."""
    key = name.strip().lower()

    # Búsqueda exacta
    if key in index:
        return index[key]

    # Búsqueda con limpieza de paréntesis
    # Ej: "Griselbrand (Retro Frame)" → buscar "griselbrand"
    import re
    key_clean = re.sub(r'\s*\([^)]*\)', '', key).strip()
    if key_clean != key and key_clean in index:
        return index[key_clean]

    # No hacer búsqueda parcial — evita matches incorrectos
    # ej: "Ornithopter" NO debe matchear "Ornithopter of Paradise"
    return None


# ─── ENDPOINTS ────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    """Endpoint de salud — verifica que el servidor está corriendo."""
    return jsonify({
        'status': 'ok',
        'products_cached': len(catalog_cache['products']),
        'cache_age_minutes': round((time.time() - catalog_cache['loaded_at']) / 60, 1),
        'loading': catalog_cache['loading']
    })


@app.route('/search', methods=['POST'])
def search():
    """
    Busca cartas en el catálogo.
    
    Body JSON: { "cards": ["Lightning Bolt", "Sol Ring", ...] }
    Respuesta: { "found": [...], "not_found": [...] }
    """
    data = request.get_json()
    if not data or 'cards' not in data:
        return jsonify({'error': 'Debes enviar una lista de cartas'}), 400

    card_names = data['cards']  # lista de strings con nombres de cartas
    index = get_catalog()

    found = []
    not_found = []

    for name in card_names:
        versions = search_in_index(name, index)
        if versions:
            # Ordenar por precio ascendente
            sorted_versions = sorted(versions, key=lambda p: p.get('price', 0))
            found.append({
                'name': name,
                'versions': sorted_versions
            })
        else:
            not_found.append(name)

    return jsonify({
        'found': found,
        'not_found': not_found,
        'total_found': len(found),
        'total_not_found': len(not_found)
    })


@app.route('/reload', methods=['POST'])
def reload_catalog():
    """Fuerza una recarga del catálogo (útil para pruebas)."""
    with cache_lock:
        catalog_cache['loaded_at'] = 0  # invalida el caché
    thread = threading.Thread(target=load_catalog, daemon=True)
    thread.start()
    return jsonify({'message': 'Recarga iniciada'})


# ─── INICIO ───────────────────────────────────────────────────
if __name__ == '__main__':
    # Pre-cargar el catálogo al iniciar
    print('[App] Iniciando servidor...')
    thread = threading.Thread(target=load_catalog, daemon=True)
    thread.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)

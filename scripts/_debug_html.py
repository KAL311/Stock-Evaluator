import json, os

reports_dir = 'reports'
files = [f for f in os.listdir(reports_dir) if f.endswith('.html')]
latest = sorted(files)[-1]
path = os.path.join(reports_dir, latest)

with open(path, 'r', encoding='utf-8') as f:
    html = f.read()

print(f'File: {path}, Size: {os.path.getsize(path)} bytes')

# Check basic structure
print(f'Starts with DOCTYPE: {html.startswith("<!DOCTYPE")}')
print(f'Has closing html: {html.rstrip().endswith("</html>")}')

# Find DATA script
marker = '<script id="DATA" type="application/json">'
s = html.find(marker)
if s < 0:
    print('ERROR: DATA marker not found')
    # Try finding any script with DATA
    alt = html.find('id="DATA"')
    print(f'Found id="DATA" at: {alt}')
    exit(1)

e = html.find('</script>', s)
json_str = html[s+len(marker):e].strip()
print(f'JSON data: {len(json_str)} chars')

# Try parsing JSON
try:
    data = json.loads(json_str)
    print('JSON parse: OK')
except json.JSONDecodeError as e:
    print(f'JSON parse ERROR: {e}')
    print(f'Context: {json_str[max(0,e.pos-100):e.pos+100]}')

# Check the closing structure
after_data = html[e:e+200]
print(f'After DATA script: {repr(after_data[:100])}')

# Check the next script tag
next_script = html.find('<script>', e)
if next_script < 0:
    next_script = html.find('<script', e+10)
print(f'Next script at: {next_script} (relative to DATA: {next_script - s})')

# Check for CDN refs
for cdn in ['tailwindcss.com', 'unpkg.com/lightweight-charts', 'fonts.googleapis.com']:
    print(f'CDN {cdn}: {"found" if cdn in html else "MISSING"}')

# Check key JS functions
for func in ['initTabs', 'switchTab', 'renderDashboard', 'renderPortfolios', 'renderScreener', 'renderTickerDetail', 'renderBacktest', 'DOMContentLoaded']:
    print(f'JS {func}: {"found" if func in html else "MISSING"}')

# Check the last bytes of the file
print(f'\nLast 100 bytes: {repr(html[-100:])}')

# Check for obvious issues
if '<script id="DATA" type="application/json">' in html:
    print('\nDATA script tag: OK')

# Check for unescaped chars in JSON
# Count how many <script> tags exist
count = html.count('<script')
print(f'Total <script> tags: {count}')

count_end = html.count('</script>')
print(f'Total </script> tags: {count_end}')

# JSON data should not contain </script>
if '</script>' in json_str:
    print('WARNING: JSON data contains </script>! This is likely the issue!')

# Check if JSON is properly closed
if json_str.endswith('}'):
    print('JSON ends with }: OK')
else:
    print(f'JSON ends with: {repr(json_str[-50:])}')

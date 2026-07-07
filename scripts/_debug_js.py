import json, os, subprocess, tempfile

reports_dir = 'reports'
files = [f for f in os.listdir(reports_dir) if f.endswith('.html')]
latest = sorted(files)[-1]
path = os.path.join(reports_dir, latest)

with open(path, 'r', encoding='utf-8') as f:
    html = f.read()

# Find the main JS block (after DATA script)
marker1 = '<script id="DATA" type="application/json">'
marker2 = '<script>'
s1 = html.find(marker1)
s2 = html.find(marker2, s1)
e = html.find('</script>', s2)

js_code = html[s2+len('<script>'):e].strip()

print(f'JS length: {len(js_code)} chars')

# Write JS to temp file and check with Node
tmp = os.path.join(tempfile.gettempdir(), '_check_syntax.js')
with open(tmp, 'w', encoding='utf-8') as f:
    f.write('// Check syntax only\n')
    f.write(js_code)

# Check with node
result = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
if result.returncode == 0:
    print('Node.js syntax check: OK')
else:
    print(f'Node.js syntax ERROR: {result.stderr[:2000]}')

os.remove(tmp)

# Check for common problematic patterns in the JS
issues = []
if '\u2014' in js_code:
    # Em dash in JS string - check if properly escaped
    print('Has em dash characters in JS (should be OK in strings)')
if '\\u2014' in js_code:
    print('Has escaped unicode em dashes (OK)')
if 'undefined' in js_code[:100]:
    pass

# Check for missing commas, brackets etc.
# Look at the first function definition
idx = js_code.find('function fmtPct')
print(f'\nFirst ~300 chars of JS:')
print(js_code[:300])

# Count braces
open_brace = js_code.count('{')
close_brace = js_code.count('}')
open_paren = js_code.count('(')
close_paren = js_code.count(')')
print(f'\nBraces: {{ = {open_brace}, }} = {close_brace}, diff = {open_brace - close_brace}')
print(f'Parens: ( = {open_paren}, ) = {close_paren}, diff = {open_paren - close_paren}')

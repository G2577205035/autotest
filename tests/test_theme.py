import re
import shutil
import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "src/auto_test/static"


class ThemeTests(unittest.TestCase):
    def test_saved_preference_first_paint_toggle_storage_sync_and_unavailable_storage(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for browser preference behavior")
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
function boot(saved,blocked=false){
  let value=saved;const events={},changes=[];
  const buttons=[0,1].map(()=>({attrs:{},label:{},setAttribute(k,v){this.attrs[k]=v},querySelector(){return this.label}}));
  const root={dataset:{},style:{}};
  const context={document:{documentElement:root,querySelectorAll:()=>buttons,addEventListener:(k,f)=>events[k]=f},
    window:{addEventListener:(k,f)=>events[k]=f,dispatchEvent:e=>changes.push(e.detail.theme)},
    CustomEvent:function(type,options){this.type=type;this.detail=options.detail},
    localStorage:{getItem(){if(blocked)throw Error('disabled');return value},setItem(k,v){assert.equal(k,'liema.theme');if(blocked)throw Error('disabled');value=v}}};
  vm.runInNewContext(source,context);
  return {root,buttons,events,changes,get saved(){return value},external(v){value=v;events.storage({key:'liema.theme'})},click(){events.click({target:{closest:()=>buttons[0]}})}};
}
let page=boot('light');assert.equal(page.root.dataset.theme,'light');assert.equal(page.root.style.colorScheme,'light');
assert(page.buttons.every(b=>b.attrs['aria-label']==='切换为深色模式'));
page.click();assert.equal(page.saved,'dark');assert.equal(page.root.dataset.theme,'dark');assert.deepEqual(page.changes,['light','dark']);
page=boot(page.saved);assert.equal(page.root.dataset.theme,'dark');
page.external('light');assert.equal(page.root.dataset.theme,'light');assert(page.buttons.every(b=>b.label.textContent==='深色模式'));
page.events.storage({key:'unrelated'});assert.equal(page.root.dataset.theme,'light');
for(const saved of [null,'invalid','<script>'])assert.equal(boot(saved).root.dataset.theme,'light');
page=boot(null,true);page.click();assert.equal(page.root.dataset.theme,'dark');assert.equal(page.saved,null);
"""
        result = subprocess.run([node, "-e", script, str(STATIC / "theme.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_theme_tokens_cover_legacy_colors_and_keep_readable_light_statuses(self):
        palette = (STATIC / "theme.css").read_text(encoding="utf-8")
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        required = set(re.findall(r"var\((--theme-[\w-]+),", css))
        values = dict(re.findall(r"(--theme-[\w-]+):\s*([^;]+);", palette))
        self.assertFalse(required - values.keys())
        # Indicator fills must stay distinct from their pale tracks in light mode.
        for selector in (".gpu-memory-bar>i", ".stress-live-progress>i", ".translation-speed-progress i",
                         ".evaluation-run-progress>i>b", ".evaluation-progress .progress-track i"):
            body = re.search(re.escape(selector) + r"\{([^}]+)\}", css).group(1)
            self.assertNotRegex(body, r"--theme-(?:accent|success|warning|danger)-soft")

        def luminance(color):
            if len(color) == 4:
                color = "#" + "".join(ch * 2 for ch in color[1:])
            parts = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in parts]
            return sum(x * y for x, y in zip(linear, (.2126, .7152, .0722)))

        for foreground, background in [("text", "surface"), ("muted", "inset"), ("accent", "selected"),
                                       ("success", "success-soft"), ("warning", "warning-soft"),
                                       ("danger", "danger-soft"), ("on-fill", "fill")]:
            with self.subTest(foreground=foreground):
                a, b = sorted([luminance(values["--theme-" + foreground]), luminance(values["--theme-" + background])])
                self.assertGreaterEqual((b + .05) / (a + .05), 4.5)

    def test_preference_bootstrap_precedes_styles_and_is_available_before_sign_in(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('/static/theme.js'), html.index('/static/styles.css'))
        self.assertEqual(html.count('data-theme-toggle '), 2)
        self.assertIn('auth-theme-toggle', html)
        self.assertIn('liema:themechange', (STATIC / "app.js").read_text(encoding="utf-8"))

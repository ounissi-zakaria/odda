(function () {
  if (window._oddaDialogs) return;
  window._oddaDialogs = true;

  var P = '_odda_d_', c = 0;

  function e(t) {
    var d = document.createElement('div');
    d.appendChild(document.createTextNode(t));
    return d.innerHTML;
  }

  function g() {
    var x = document.getElementById(P + 'ct');
    if (x) return x;
    x = document.createElement('div');
    x.id = P + 'ct';
    x.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:2147483647;pointer-events:none;font-family:-apple-system,BlinkMacSystemFont,sans-serif;';
    document.documentElement.appendChild(x);
    return x;
  }

  function m(msg, type, val) {
    c++;
    var id = P + 'm' + c;
    var h = '<div id="' + id + '" style="position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:auto;z-index:2147483647;">'
      + '<div style="position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.4);"></div>'
      + '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);'
      + 'background:#fff;border:2px solid #333;border-radius:8px;padding:20px 24px;'
      + 'max-width:400px;min-width:250px;box-shadow:0 4px 32px rgba(0,0,0,0.3);">'
      + '<div style="font-weight:600;margin-bottom:8px;color:#111;font-size:14px;">Page says</div>'
      + '<div style="margin-bottom:16px;color:#222;font-size:13px;word-break:break-word;line-height:1.5;">' + e(String(msg)) + '</div>';

    if (type === 'prompt') {
      h += '<input id="' + P + 'i' + c + '" type="text" value="' + e(String(val != null ? val : '')) + '"'
        + ' style="width:100%;padding:6px 8px;border:1px solid #888;border-radius:4px;margin-bottom:12px;box-sizing:border-box;font-size:13px;color:#222;">';
    }

    h += '<div style="text-align:right;">'
      + '<button style="padding:4px 16px;border:1px solid #666;border-radius:4px;background:#e8e8e8;cursor:pointer;font-size:13px;font-weight:500;color:#111;" onclick="(function(){document.getElementById(\'' + id + '\').remove()})()">OK</button>';

    if (type === 'confirm') {
      h += '<button style="padding:4px 16px;border:1px solid #666;border-radius:4px;background:#e8e8e8;cursor:pointer;margin-left:8px;font-size:13px;font-weight:500;color:#111;" onclick="(function(){document.getElementById(\'' + id + '\').remove()})()">Cancel</button>';
    }

    h += '</div></div></div>';
    g().insertAdjacentHTML('beforeend', h);
  }

  window.alert = function (a) { m(a, 'alert') };
  window.confirm = function (a) { m(a, 'confirm'); return true };
  window.prompt = function (a, b) { m(a, 'prompt', b); return b != null ? b : null };
})();

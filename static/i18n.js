(function () {
  var config = window.fakturekI18n || {};
  var lang = String(config.language || "cs").toLowerCase();
  if (lang !== "en") return;

  var translations = config.translations || {};
  var attributeTranslations = config.attributeTranslations || translations;
  var textKeys = Object.keys(translations);
  if (!textKeys.length) return;

  var SKIP_SELECTOR = [
    "script",
    "style",
    "template",
    "svg",
    "canvas",
    "code",
    "pre",
    "kbd",
    "samp",
    "textarea",
    "input",
    "select",
    "option",
    "[data-i18n-skip]",
    "[data-no-translate]",
    ".invoice-paper",
    ".pdf-page",
    ".pdf-sheet",
    ".public-invoice-paper"
  ].join(",");

  function normalize(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function preserveWhitespace(original, replacement) {
    var leading = (String(original).match(/^\s*/) || [""])[0];
    var trailing = (String(original).match(/\s*$/) || [""])[0];
    return leading + replacement + trailing;
  }

  function closestSkipped(node) {
    var element = node && (node.nodeType === 1 ? node : node.parentElement);
    return !!(element && element.closest && element.closest(SKIP_SELECTOR));
  }

  function translateExactTextNode(node) {
    if (!node || !node.nodeValue || closestSkipped(node)) return;
    var normalized = normalize(node.nodeValue);
    if (!normalized) return;
    var translated = translations[normalized];
    if (!translated) return;
    node.nodeValue = preserveWhitespace(node.nodeValue, translated);
  }

  function translateAttributes(root) {
    var attrNames = ["title", "aria-label", "placeholder", "data-copy-label", "data-bs-original-title"];
    var elements = [];
    if (root && root.nodeType === 1) elements.push(root);
    if (root && root.querySelectorAll) {
      attrNames.forEach(function (attrName) {
        root.querySelectorAll("[" + attrName + "]").forEach(function (element) {
          elements.push(element);
        });
      });
      root.querySelectorAll('input[type="submit"][value], input[type="button"][value]').forEach(function (element) {
        elements.push(element);
      });
    }

    elements.forEach(function (element) {
      if (!element || closestSkipped(element)) return;
      attrNames.forEach(function (attrName) {
        if (!element.hasAttribute || !element.hasAttribute(attrName)) return;
        var raw = element.getAttribute(attrName);
        var translated = attributeTranslations[normalize(raw)];
        if (translated) element.setAttribute(attrName, preserveWhitespace(raw, translated));
      });
      if (element.tagName === "INPUT" && /^(submit|button)$/i.test(element.type || "")) {
        var rawValue = element.getAttribute("value") || "";
        var translatedValue = attributeTranslations[normalize(rawValue)];
        if (translatedValue) element.setAttribute("value", preserveWhitespace(rawValue, translatedValue));
      }
    });
  }

  function translateTree(root) {
    root = root || document.body;
    if (!root || closestSkipped(root)) return;
    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode: function (node) {
          if (!normalize(node.nodeValue)) return NodeFilter.FILTER_REJECT;
          if (closestSkipped(node)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(translateExactTextNode);
    translateAttributes(root);
  }

  function schedule(root) {
    window.requestAnimationFrame(function () {
      translateTree(root || document.body);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { schedule(document.body); });
  } else {
    schedule(document.body);
  }

  try {
    var observer = new MutationObserver(function (mutations) {
      mutations.forEach(function (mutation) {
        mutation.addedNodes && mutation.addedNodes.forEach(function (node) {
          if (node.nodeType === 1 || node.nodeType === 3) schedule(node.nodeType === 1 ? node : node.parentElement);
        });
      });
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  } catch (error) {}
})();

"use strict";
if (typeof renderMathInElement === "function") {
  document.querySelectorAll("[data-math]").forEach((element) => {
    renderMathInElement(element, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
      trust: false,
      strict: "ignore",
      maxExpand: 1000,
      maxSize: 20,
    });
  });
}


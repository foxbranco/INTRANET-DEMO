const fs = require("fs");
const html = fs.readFileSync("/codigo/index-v9-teste.html", "utf8");

const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)]
    .map(m => m[1])
    .filter(s => s.trim());

let erros = 0;

scripts.forEach((script, i) => {
    try {
        new Function(script);
        console.log("SCRIPT " + (i + 1) + ": OK");
    } catch (e) {
        erros++;
        console.log("SCRIPT " + (i + 1) + ": ERRO - " + e.message);
    }
});

console.log("TOTAL DE ERROS: " + erros);

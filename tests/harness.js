// Loads worker/worker.js without changing it (so the deployed worker is untouched) and runs its pure
// functions on JSON cases read from stdin: {"fn": "trend", "args": [...]} per case. Prints JSON results.
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(__dirname + "/../worker/worker.js", "utf8").replace("export default", "const __worker =");
const ctx = vm.createContext({ console, Intl, Date, Math, URLSearchParams });
vm.runInContext(src + "\nthis.fns = { trend, fadeRead, rankScore, sessionFraction, round };", ctx);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
console.log(JSON.stringify(cases.map((c) => ctx.fns[c.fn](...c.args))));

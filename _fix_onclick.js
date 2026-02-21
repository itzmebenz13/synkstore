const fs = require('fs');
const file = 'index.html';
let html = fs.readFileSync(file, 'utf8');

// Fix: JSON.stringify(acc.id) produces "uuid" with double quotes that breaks onclick HTML attributes
// Replace with single-quote wrapped acc.id
html = html.replace(
    `'<button onclick=\"window.updateSheinAccount(' + JSON.stringify(acc.id) + ')\"`,
    `'<button onclick=\"window.updateSheinAccount(\\'' + acc.id + '\\')\"`
);
html = html.replace(
    `'<button onclick=\"window.deleteSheinAccount(' + JSON.stringify(acc.id) + ')\"`,
    `'<button onclick=\"window.deleteSheinAccount(\\'' + acc.id + '\\')\"`
);

fs.writeFileSync(file, html, 'utf8');
console.log('Done. Patched onclick attributes.');

// Runs the dashboard's times.js over <time> elements given as JSON ([[datetime, style,
// text], ...]) and prints the text each one shows afterwards. The zone is the process's (TZ).
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const elements = JSON.parse(process.argv[3]).map(([datetime, style, text]) => ({
  dataset: {time: style},
  textContent: text,
  getAttribute: name => (name === 'datetime' ? datetime : null),
}));
const document = {
  readyState: 'interactive',
  querySelectorAll: selector => {
    if (selector !== 'time[data-time][datetime]') throw new Error(`unexpected ${selector}`);
    return elements;
  },
};
const source = fs.readFileSync(path.join(process.argv[2], 'times.js'), 'utf8');
vm.runInNewContext(source, {document, Date, Math, Number, String});
process.stdout.write(JSON.stringify(elements.map(element => element.textContent)));

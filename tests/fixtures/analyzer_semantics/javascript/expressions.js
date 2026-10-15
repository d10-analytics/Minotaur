function helper() {}

const View = class {
    field = helper();
    static s = helper();
};

class Pinned {
    field = helper();
}

function make() {
    return new helper();
}

function ready() {}

async function drain() {
    while (pending) {}
    do {} while (ready);
    for await (const item of batches) {}
}

function slot() {}

function scatter(values) {
    [cache[slot()]] = values;
    ({ first: table[slot()], ...rest[slot()] } = values);
}

function pick() {}

function regather(values) {
    [pick, ledger[pick()], spare = pick()] = values;
}

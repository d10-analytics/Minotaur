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

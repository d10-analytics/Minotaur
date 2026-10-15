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

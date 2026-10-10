export class Gauge {
    get level() {
        return this._level;
    }
    set level(value) {
        this._level = value;
    }
    static get maximum() {
        return 10;
    }
}

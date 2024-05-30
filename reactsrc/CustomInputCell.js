class CustomInputCell {
    /**
     * Custom Row Cell
     * @constructor
     * @param {object} globalConfig - Global configuration.
     * @param {string} serviceName - Input service name.
     * @param {element} el - The element of the custom cell.
     * @param {object} row - The object containing current values of row.
     * @param {string} field - Custom cell field name.
     */
    constructor(globalConfig, serviceName, el, row, field) {
        this.globalConfig = globalConfig;
        this.serviceName = serviceName;
        this.el = el;
        this.row = row;
        this.field = field;
    }

    render(row, field) {
        let html = "";
        let entity = this.globalConfig.pages.inputs.services.find(
            (x) => x.name === this.serviceName
        );
        if (field == "input") {
            html = entity.title;
        } else if(field == "meter") {
            html = this.row[field].split("#")[0];
        } else {
            html = "Unknown";
        }
        this.el.innerHTML = html;
        return this;
    }
}

export default CustomInputCell;
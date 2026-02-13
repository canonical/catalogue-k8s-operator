(function() {

  fetch('config.json')
    .then(response => response.json())
    .then(data => {

      const source = document.getElementById('root-template').innerHTML;
      const template = Handlebars.compile(source)
      const rendered = template(data);

      document.getElementById('root').innerHTML = rendered;
      
    })

})()
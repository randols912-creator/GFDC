/* App Module */
var geniframework = angular.module('geniframework', []);

geniframework.config(['$routeProvider', function($routeProvider){
	$routeProvider.when('/home', {
		templateUrl : '../../static/partials/home.html',
		controller : HomeController
	});
    $routeProvider.when('/unique', {
		templateUrl : '../../static/partials/unique.html?v=3',
		controller : UniqueController
	});
/*
    $routeProvider.when('/top10', {
		templateUrl : '../../static/partials/top.html',
		controller : Top10Controller
	});
*/
    $routeProvider.when('/top50', {
        templateUrl : '../../static/partials/top50.html',
        controller : Top50Controller
    });
	$routeProvider.otherwise({
    	redirectTo : '/unique'
	});
}]);

function HomeController($scope,$rootScope, $http){
    var httpPromise = $http;
    var profileAPI = '/getProfile';
    $scope.loading = true;
    callServerGETAPI(httpPromise, profileAPI, procesSearch);

    $scope.recentProfiles = [];

    function procesSearch(responseData){
        $scope.loading = false;
        $('.loadingMask').hide();
        $scope.profileData = responseData;
        $scope.profileId = $scope.profileData.id;
        $scope.profileName = $scope.profileData.name;
    }

    $scope.getProfile = function(id, name){
        var profileAPI = 'js/json/' + id+'.js';
        $scope.loading = true;
        $('.loadingMask').show();
        callServerGETAPI(httpPromise, profileAPI, procesSearch);
        if($scope.recentProfiles.length === 0){
            var profileObj = {"id" : $scope.profileId, "name" : $scope.profileName}
            $scope.recentProfiles.push(profileObj);
        }else{
          var count = 0;
            var profileObj = {"id" : $scope.profileId, "name" : $scope.profileName};
            $.each($scope.recentProfiles, function(index, value) {
                //console.log(JSON.stringify($scope.recentProfiles));
                //console.log(value.id + "------" + id);
              if(value.id === $scope.profileId){
				 count = count + 1;
			  }
		   });
            if(count === 0){
                $scope.recentProfiles.push(profileObj);
            }
        }
    }
}

var UniqueController = function($scope,$rootScope, $http){
    var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

    $scope.showTableDataMyProfile = false;
    $scope.showTableDataOtherProfile = false;

    $scope.my = {stepValue: null, includeTop50: false, email: ''};
    $scope.other = {otherId: '', stepValue: null, includeTop50: false, email: '', selectedName: ''};
    $scope.myBusy = false;    $scope.myError = '';    $scope.myMsg = '';
    $scope.otherBusy = false; $scope.otherError = ''; $scope.otherMsg = '';
    $scope.search = {query: '', results: [], busy: false, error: '', page: 1, hasNext: false, searched: false};

    // ---- validation shared by both tabs; returns an error string or '' ----
    function validate(model, needsId){
        var s = parseInt(model.stepValue, 10);
        if(isNaN(s) || s < 1 || s > 10){
            return 'Please enter a number of steps between 1 and 10.';
        }
        if(needsId && !String(model.otherId || '').replace(/\D/g, '').length){
            return 'Please pick a profile (search above) or enter a profile ID.';
        }
        if(s >= 4 && !EMAIL_RE.test(model.email || '')){
            return 'Runs of 4 or more steps are emailed to you step by step - please enter a valid email address.';
        }
        return '';
    }

    // ---- pull the numeric id out of a pasted geni.com URL ----
    $scope.idTyped = function(){
        $scope.other.selectedName = '';
        var raw = String($scope.other.otherId || '');
        if(raw.indexOf('geni.com') !== -1 || /[^0-9]/.test(raw)){
            var m = raw.match(/(\d{5,})/g);
            if(m && m.length){
                $scope.other.otherId = m[m.length - 1];
            }
        }
    };

    // ---- profile search (Geni profile/search via our /searchProfiles) ----
    $scope.runSearch = function(page){
        var q = (($scope.search.query) || '').replace(/^\s+|\s+$/g, '');
        if(!q){ return false; }
        $scope.search.busy = true;
        $scope.search.error = '';
        $http.get('/searchProfiles', {params: {names: q, page: page || 1}})
            .success(function(data){
                $scope.search.busy = false;
                $scope.search.searched = true;
                $scope.search.results = data.results || [];
                $scope.search.page = data.page || 1;
                $scope.search.hasNext = !!data.has_next;
            })
            .error(function(data, status){
                $scope.search.busy = false;
                $scope.search.searched = true;
                $scope.search.results = [];
                $scope.search.error = (status === 401) ?
                    'Your session expired - please log in again.' :
                    'Search failed - please try again in a moment.';
            });
        return false;
    };

    $scope.handleSearchKey = function($event){
        if($event.keyCode === 13){
            $event.preventDefault();
            $scope.runSearch(1);
        }
    };

    $scope.selectProfile = function(p){
        $scope.other.otherId = p.guid;
        $scope.other.selectedName = p.name;
        $scope.otherError = '';
    };

    // ---- submit: My Profile tab ----
    $scope.submitMy = function(){
        $scope.myError = ''; $scope.myMsg = '';
        var err = validate($scope.my, false);
        if(err){ $scope.myError = err; return; }
        var s = parseInt($scope.my.stepValue, 10);
        var params = {stepCount: s, myProfile: 'true'};
        if($scope.my.includeTop50){ params.includeTop50 = 'on'; }
        if(s >= 4){ params.email = $scope.my.email; }
        $scope.myBusy = true;
        $http.get('/getUniqueCount', {params: params})
            .success(function(data){ finish('my', data); })
            .error(function(){ fail('my'); });
    };

    // ---- submit: Another Profile tab ----
    $scope.submitOther = function(){
        $scope.otherError = ''; $scope.otherMsg = '';
        var err = validate($scope.other, true);
        if(err){ $scope.otherError = err; return; }
        var s = parseInt($scope.other.stepValue, 10);
        var params = {stepCount: s, myProfile: 'false',
                      otherId: String($scope.other.otherId).replace(/\D/g, '')};
        if($scope.other.includeTop50){ params.includeTop50 = 'on'; }
        if(s >= 4){ params.email = $scope.other.email; }
        $scope.otherBusy = true;
        $http.get('/getUniqueCount', {params: params})
            .success(function(data){ finish('other', data); })
            .error(function(){ fail('other'); });
    };

    function finish(which, data){
        var msg = data && data.backgroundMessage;
        if(which === 'my'){
            $scope.myBusy = false;
            if(msg){ $scope.myMsg = msg; }
            else { $scope.myProfileData = data; $scope.showTableDataMyProfile = true; }
        } else {
            $scope.otherBusy = false;
            if(msg){ $scope.otherMsg = msg; }
            else { $scope.otherProfileData = data; $scope.showTableDataOtherProfile = true; }
        }
    }

    function fail(which){
        var msg = 'Something went wrong - your login may have expired. ' +
                  'Please reload the page and log in again.';
        if(which === 'my'){ $scope.myBusy = false; $scope.myError = msg; }
        else { $scope.otherBusy = false; $scope.otherError = msg; }
    }
};

var Top10Controller = function($scope,$rootScope, $http){
    var httpPromise = $http;
    $scope.loading = true;
    $('.loadingMask').show();
    var top10ProfileData = '/top10';
    callServerGETAPI(httpPromise, top10ProfileData, showTop10Profiles);

    function showTop10Profiles(responseData){
        $scope.loading = false;
        $('.loadingMask').hide();
        $scope.top10Profiles = responseData.top10;
    }

};

var Top50Controller = function($scope,$rootScope, $http){
    var httpPromise = $http;
    var me = this;
    var getTopTenSteps =  '../../static/js/steps.js';
    callServerGETAPI(httpPromise, getTopTenSteps, showTop10Steps);
    $scope.selected = -1;
    function showTop10Steps(data){
        $scope.top10Steps = data.steps;
        //stepProfileData
    }

    $scope.showProfileData = function(stepValue, index){
        var getProfilesForStep = '/top50?stepValue='+stepValue;
        $scope.selected = index;
        console.log(index);
        $scope.loading = true;
        $('.loadingMask').show();
        callServerGETAPI(httpPromise, getProfilesForStep, me.showProfilesData);
    };

    me.showProfilesData = function(data){
        $scope.stepProfileData = data.top50;
        $scope.loading = false;
        $('.loadingMask').hide();
        $scope.showResults = true;
    };
};

function callServerGETAPI(httpPromise, apiName, reponseHandler){
	httpPromise.get(apiName).success(reponseHandler);
}

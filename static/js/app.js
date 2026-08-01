/* App Module */
var geniframework = angular.module('geniframework', []);

geniframework.config(['$routeProvider', function($routeProvider){
	$routeProvider.when('/home', {
		templateUrl : '../../static/partials/home.html',
		controller : HomeController
	});
    $routeProvider.when('/unique', {
		templateUrl : '../../static/partials/unique.html',
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
    var httpPromise = $http;
    $('#uniqueProfilesTab a[href="#profile"]').tab('show');
    $scope.showTableDataMyProfile = false;
    $scope.showTableDataOtherProfile = false;
    $scope.submitMyProfile = function(formId){
        var getFormData = $(formId).serialize();
        $rootScope.formId = formId;
        var submiProfileAPI = '/getUniqueCount?'+getFormData;
        if($rootScope.formId === '#myProfileForm'){
        	if($scope.myProfileForm.stepValue > 10){
        		alert('Please enter steps between 1 to 10.');
        		$scope.myProfileForm.stepValue = '';
        		$scope.myProfileForm.emailField = '';
        		return false;
        	}
           if($scope.myProfileForm.stepValue < 4){
                if($scope.myProfileForm.stepValue !== ''){
                    $scope.loading = true;
                    $('.loadingMask').show();
                    callServerGETAPI(httpPromise, submiProfileAPI, showTableData);
                }
           }else{
                if(($scope.myProfileForm.stepValue !== '') && ($scope.myProfileForm.emailField !== '')
                   && ($scope.myProfileForm.email.$valid)){
                    $scope.loading = true;
                    $('.loadingMask').show();
                    callServerGETAPI(httpPromise, submiProfileAPI, showTableData);
                }
           }
        }else{
        	//Other form
        	if($scope.otherProfileForm.stepValue > 10){
        		alert('Please enter steps between 1 to 10.');
        		$scope.otherProfileForm.stepValue = '';
        		$scope.otherProfileForm.emailField = '';
        		return false;
        	}

            if($scope.otherProfileForm.stepValue < 4){
                if($scope.otherProfileForm.stepValue !== ''){
                    $scope.loading = true;
                    $('.loadingMask').show();
                    callServerGETAPI(httpPromise, submiProfileAPI, showTableData);
                }
            }else{
                if(($scope.otherProfileForm.stepValue !== '') && ($scope.otherProfileForm.emailField !== '')
                   && ($scope.otherProfileForm.email.$valid)){
                    $scope.loading = true;
                    $('.loadingMask').show();
                    callServerGETAPI(httpPromise, submiProfileAPI, showTableData);
                }
            }
        }
    }

    function showTableData(responseData){
        $scope.loading = false;
        $('.loadingMask').hide();
        if($rootScope.formId === '#otherProfileForm'){
            $scope.otherProfileData = responseData;
            if(! angular.isUndefined($scope.otherProfileData.backgroundMessage)){
                $scope.otherProfileFormSuccessMsg = true;
                $('#otherProfileFormSuccessMsg').html($scope.otherProfileData.backgroundMessage);
                $('#otherProfileFormSuccessMsg').css("background-color","#00BFFF");
                setTimeout(function(){
                    $scope.otherProfileFormSuccessMsg = false;
                    $('#otherProfileFormSuccessMsg').fadeOut('slow');
                }, 5000);
            };
            $scope.showTableDataOtherProfile = true;
            $scope.otherProfileForm.stepValue = null;
            $scope.otherProfileForm.emailField = null;
        }else{
            $scope.myProfileData = responseData;
            console.log(!angular.isUndefined($scope.myProfileData.backgroundMessage));
            if(!angular.isUndefined($scope.myProfileData.backgroundMessage)){
                $scope.myProfileFormSuccessMsg = true;
                $('#myProfileFormSuccessMsg').html($scope.myProfileData.backgroundMessage);
                $('#myProfileFormSuccessMsg').css("background-color","#00BFFF");
                setTimeout(function(){
                    $scope.myProfileFormSuccessMsg = false;
                    $('#myProfileFormSuccessMsg').fadeOut('slow');
                }, 5000);
            };
            $scope.showTableDataMyProfile = true;
            $scope.myProfileForm.stepValue = null;
            $scope.myProfileForm.emailField = null;
        }
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
